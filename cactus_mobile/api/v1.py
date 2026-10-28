"""Cactus Employee mobile app API, version 1.

Every method is reached at /api/method/cactus_mobile.api.v1.<name>. Apart from app_config and
login, calls need the token from login in the header:  Authorization: token <api_key>:<api_secret>

Each call acts for the Employee linked to the calling user, never for an employee named in the
request, so one employee cannot read or write another's records. Business rules (shift windows,
GPS and geofence checks, leave balances, approvers) stay with Frappe HR: this module only shapes
its data for the app.
"""

import base64

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, get_last_day, getdate, now_datetime, nowdate

API_VERSION = 1
CHECKIN_DEVICE_ID = "Cactus Mobile"
MAX_RECEIPT_BYTES = 5 * 1024 * 1024

EMPLOYEE_FIELDS = [
	"name",
	"employee_name",
	"first_name",
	"company",
	"department",
	"designation",
	"image",
	"date_of_joining",
	"cell_number",
	"company_email",
	"user_id",
]


# ---- helpers ----


def _settings():
	return frappe.get_cached_doc("Cactus Mobile Settings")


def _ensure_enabled():
	if not cint(_settings().enabled):
		frappe.throw(_("The mobile app is turned off for this company."), frappe.PermissionError)


def _employee() -> frappe._dict:
	"""The active Employee linked to the calling user."""
	_ensure_enabled()
	employee = frappe.db.get_value(
		"Employee", {"user_id": frappe.session.user, "status": "Active"}, EMPLOYEE_FIELDS, as_dict=True
	)
	if not employee:
		frappe.throw(_("Your login is not linked to an active employee."), frappe.PermissionError)
	return employee


def _profile(employee: frappe._dict) -> dict:
	return {
		**employee,
		"currency": frappe.get_cached_value("Company", employee.company, "default_currency"),
		"shift": _shift_for(employee.name),
	}


def _shift_for(employee: str, timestamp=None) -> dict | None:
	from hrms.hr.doctype.shift_assignment.shift_assignment import get_employee_shift

	shift = get_employee_shift(employee, timestamp or now_datetime(), consider_default_shift=True)
	if not shift:
		return None
	return {
		"name": shift.shift_type.name,
		"start": shift.start_datetime,
		"end": shift.end_datetime,
	}


def _api_token(user: str) -> str:
	"""The user's API key and secret, created on first login and reused after, so an
	employee can stay signed in on more than one device."""
	user_doc = frappe.get_doc("User", user)
	secret = user_doc.get_password("api_secret", raise_exception=False) if user_doc.api_key else None
	if not secret:
		secret = frappe.generate_hash(length=15)
		user_doc.api_key = user_doc.api_key or frappe.generate_hash(length=15)
		user_doc.api_secret = secret
		user_doc.save(ignore_permissions=True)
	return f"{user_doc.api_key}:{secret}"


def _own(doctype: str, name: str, employee: str, submitted: bool = False):
	values = frappe.db.get_value(doctype, name, ["employee", "docstatus"], as_dict=True)
	if not values or values.employee != employee or (submitted and values.docstatus != 1):
		frappe.throw(_("{0} {1} not found").format(_(doctype), name), frappe.DoesNotExistError)


# ---- app and session ----


@frappe.whitelist(allow_guest=True, methods=["GET"])
def app_config() -> dict:
	"""Public: lets the login screen check the server address and show the company's branding."""
	website = frappe.get_single("Website Settings")
	return {
		"api_version": API_VERSION,
		"enabled": cint(_settings().enabled),
		"min_app_version": _settings().min_app_version,
		"app_name": website.app_name,
		"logo": website.app_logo,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=10 * 60)
def login(usr: str, pwd: str) -> dict:
	"""Check the employee's password and return the token the app sends with every call."""
	from frappe.auth import LoginManager

	_ensure_enabled()
	login_manager = LoginManager()
	login_manager.authenticate(user=usr, pwd=pwd)
	frappe.set_user(login_manager.user)
	employee = _employee()
	return {"token": _api_token(login_manager.user), "employee": _profile(employee)}


@frappe.whitelist(methods=["POST"])
def sign_out_all_devices() -> dict:
	"""Replace the API secret, so every device signed in as this user must log in again."""
	_employee()
	user_doc = frappe.get_doc("User", frappe.session.user)
	user_doc.api_secret = frappe.generate_hash(length=15)
	user_doc.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist(methods=["GET"])
def profile() -> dict:
	return _profile(_employee())


# ---- check-in ----


@frappe.whitelist(methods=["GET"])
def checkin_status() -> dict:
	employee = _employee()
	today = frappe.get_all(
		"Employee Checkin",
		filters={"employee": employee.name, "time": [">=", getdate(nowdate())]},
		fields=["name", "log_type", "time", "latitude", "longitude"],
		order_by="time asc",
	)
	return {
		"today": today,
		"next_action": "OUT" if today and today[-1].log_type == "IN" else "IN",
		"shift": _shift_for(employee.name),
		"location_required": cint(frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking")),
		"max_gps_accuracy": cint(_settings().max_gps_accuracy),
		"server_time": now_datetime(),
	}


@frappe.whitelist(methods=["POST"])
def checkin(log_type: str, latitude=None, longitude=None, accuracy=None) -> dict:
	"""Record a check-in or check-out at the server's current time.

	Frappe HR requires the location when HR Settings has geolocation tracking on, and enforces
	the office radius when the employee's shift assignment has a Shift Location.
	"""
	employee = _employee()
	if log_type not in ("IN", "OUT"):
		frappe.throw(_("Log type must be IN or OUT."))

	max_accuracy = cint(_settings().max_gps_accuracy)
	if max_accuracy and accuracy is not None and flt(accuracy) > max_accuracy:
		frappe.throw(
			_("Your location is not accurate enough ({0} m). Move outdoors or near a window and try again.").format(
				cint(accuracy)
			)
		)

	doc = frappe.get_doc(
		{
			"doctype": "Employee Checkin",
			"employee": employee.name,
			"log_type": log_type,
			"time": now_datetime(),
			"latitude": flt(latitude) if latitude is not None else None,
			"longitude": flt(longitude) if longitude is not None else None,
			"device_id": CHECKIN_DEVICE_ID,
		}
	)
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "log_type": doc.log_type, "time": doc.time}


# ---- attendance ----


@frappe.whitelist(methods=["GET"])
def attendance(month: str | None = None) -> dict:
	"""A month of attendance (month as YYYY-MM, default this month) with holidays and totals."""
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

	employee = _employee()
	start = getdate(f"{month}-01") if month else getdate(nowdate()).replace(day=1)
	end = get_last_day(start)

	days = frappe.get_all(
		"Attendance",
		filters={"employee": employee.name, "docstatus": 1, "attendance_date": ["between", [start, end]]},
		fields=[
			"attendance_date",
			"status",
			"working_hours",
			"late_entry",
			"early_exit",
			"in_time",
			"out_time",
			"leave_type",
			"shift",
		],
		order_by="attendance_date asc",
	)

	holidays = []
	# with Frappe HR, holiday lists come from Holiday List Assignments and can change over time
	holiday_list = get_holiday_list_for_employee(employee.name, raise_exception=False, as_on=start)
	if holiday_list:
		holidays = frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "holiday_date": ["between", [start, end]]},
			fields=["holiday_date", "description", "weekly_off"],
			order_by="holiday_date asc",
		)

	summary = {"present": 0, "absent": 0, "half_day": 0, "on_leave": 0, "work_from_home": 0, "late": 0, "hours": 0.0}
	for day in days:
		key = day.status.lower().replace(" ", "_")
		if key in summary:
			summary[key] += 1
		summary["late"] += cint(day.late_entry)
		summary["hours"] += flt(day.working_hours)
	summary["hours"] = flt(summary["hours"], 2)

	return {"month": start.strftime("%Y-%m"), "days": days, "holidays": holidays, "summary": summary}


# ---- leave ----


@frappe.whitelist(methods=["GET"])
def leave_balances() -> list[dict]:
	from hrms.hr.doctype.leave_application.leave_application import get_leave_details

	employee = _employee()
	details = get_leave_details(employee.name, nowdate())
	balances = [
		{
			"leave_type": leave_type,
			"allocated": flt(row.get("total_leaves")),
			"taken": flt(row.get("leaves_taken")),
			"pending": flt(row.get("leaves_pending_approval")),
			"remaining": flt(row.get("remaining_leaves")),
			"without_pay": 0,
		}
		for leave_type, row in (details.get("leave_allocation") or {}).items()
	]
	# leave without pay needs no allocation, so it is always available to apply for
	balances += [
		{"leave_type": lwp, "allocated": 0, "taken": 0, "pending": 0, "remaining": 0, "without_pay": 1}
		for lwp in details.get("lwps") or []
	]
	return balances


@frappe.whitelist(methods=["GET"])
def leave_applications(limit: int = 50) -> list[dict]:
	employee = _employee()
	return frappe.get_all(
		"Leave Application",
		filters={"employee": employee.name, "docstatus": ["!=", 2]},
		fields=[
			"name",
			"leave_type",
			"from_date",
			"to_date",
			"half_day",
			"total_leave_days",
			"status",
			"docstatus",
			"description",
			"posting_date",
		],
		order_by="from_date desc",
		limit=min(cint(limit) or 50, 200),
	)


@frappe.whitelist(methods=["POST"])
def apply_leave(
	leave_type: str,
	from_date: str,
	to_date: str,
	half_day: int = 0,
	half_day_date: str | None = None,
	reason: str | None = None,
) -> dict:
	"""Create an open Leave Application for the employee's leave approver to act on."""
	from hrms.hr.doctype.leave_application.leave_application import get_leave_approver

	employee = _employee()
	doc = frappe.get_doc(
		{
			"doctype": "Leave Application",
			"employee": employee.name,
			"company": employee.company,
			"leave_type": leave_type,
			"from_date": from_date,
			"to_date": to_date,
			"half_day": cint(half_day),
			"half_day_date": (half_day_date or from_date) if cint(half_day) else None,
			"description": reason,
			"leave_approver": get_leave_approver(employee.name),
			"status": "Open",
			"posting_date": nowdate(),
			"follow_via_email": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "status": doc.status, "total_leave_days": doc.total_leave_days}


# ---- pay ----


@frappe.whitelist(methods=["GET"])
def salary_slips(limit: int = 24) -> list[dict]:
	employee = _employee()
	return frappe.get_all(
		"Salary Slip",
		filters={"employee": employee.name, "docstatus": 1},
		fields=[
			"name",
			"start_date",
			"end_date",
			"posting_date",
			"currency",
			"gross_pay",
			"total_deduction",
			"net_pay",
		],
		order_by="start_date desc",
		limit=min(cint(limit) or 24, 120),
	)


@frappe.whitelist(methods=["GET"])
def salary_slip_pdf(name: str):
	"""Download a submitted salary slip as PDF."""
	employee = _employee()
	_own("Salary Slip", name, employee.name, submitted=True)
	frappe.local.response.filename = f"{name}.pdf"
	frappe.local.response.filecontent = frappe.get_print("Salary Slip", name, as_pdf=True)
	frappe.local.response.type = "pdf"


@frappe.whitelist(methods=["GET"])
def expense_claim_types() -> list[str]:
	_employee()
	return frappe.get_all("Expense Claim Type", pluck="name", order_by="name asc")


@frappe.whitelist(methods=["GET"])
def expense_claims(limit: int = 50) -> list[dict]:
	employee = _employee()
	return frappe.get_all(
		"Expense Claim",
		filters={"employee": employee.name, "docstatus": ["!=", 2]},
		fields=[
			"name",
			"posting_date",
			"currency",
			"total_claimed_amount",
			"total_sanctioned_amount",
			"status",
			"approval_status",
			"docstatus",
		],
		order_by="posting_date desc, creation desc",
		limit=min(cint(limit) or 50, 200),
	)


@frappe.whitelist(methods=["POST"])
def submit_expense_claim(
	expense_type: str,
	amount: float,
	expense_date: str,
	description: str | None = None,
	receipt: str | None = None,
	receipt_filename: str | None = None,
) -> dict:
	"""Create a draft Expense Claim for the employee's expense approver, with an optional receipt
	photo sent as base64. The approver reviews, sanctions and submits it in ERPNext."""
	employee = _employee()
	if flt(amount) <= 0:
		frappe.throw(_("Amount must be more than zero."))

	content = None
	if receipt:
		content = base64.b64decode(receipt.split(",", 1)[-1])
		if len(content) > MAX_RECEIPT_BYTES:
			frappe.throw(_("The receipt photo is too large. The limit is 5 MB."))

	company_currency = frappe.get_cached_value("Company", employee.company, "default_currency")
	doc = frappe.get_doc(
		{
			"doctype": "Expense Claim",
			"employee": employee.name,
			"company": employee.company,
			"currency": company_currency,
			"exchange_rate": 1,
			"posting_date": nowdate(),
			"expense_approver": frappe.db.get_value("Employee", employee.name, "expense_approver"),
			"expenses": [
				{
					"expense_date": expense_date,
					"expense_type": expense_type,
					"description": description or expense_type,
					"amount": flt(amount),
					"sanctioned_amount": flt(amount),
				}
			],
		}
	)
	doc.insert(ignore_permissions=True)

	if content:
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": receipt_filename or f"receipt-{doc.name}.jpg",
				"attached_to_doctype": "Expense Claim",
				"attached_to_name": doc.name,
				"content": content,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)

	return {"name": doc.name, "status": doc.status, "total_claimed_amount": doc.total_claimed_amount}
