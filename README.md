### Cactus Mobile

ERPNext connector for the **Cactus Employee** mobile app: a versioned, token-secured API over
Frappe HR, in its own Frappe app so it can be installed, updated and turned off separately from
the theme and from ERPNext itself.

Requires `erpnext` and `hrms` (Frappe HR), branch `version-16`.

```bash
bench get-app <repo-url> --branch version-16
bench --site <site> install-app cactus_mobile
```

#### Settings

**Cactus Mobile Settings** (search for it in the desk):

- **Enable mobile app**: when off, no employee can sign in or use the API.
- **Maximum GPS accuracy (metres)**: check-ins are refused when the phone reports a less accurate
  location (default 100, 0 = no limit).
- **Minimum app version**: for asking employees with old app versions to update.

The GPS requirement and the office radius are Frappe HR's own: HR Settings → "Allow Geolocation
Tracking", and a Shift Location on the employee's Shift Assignment.

#### API

All methods are at `/api/method/cactus_mobile.api.v1.<name>`. `app_config` and `login` are public;
every other call needs the header `Authorization: token <api_key>:<api_secret>` returned by login.
Each call acts for the Employee linked to the signed-in user, never for an employee named in the
request, so one employee can't read or change another's records.

| Method | HTTP | What it does |
|---|---|---|
| `app_config` | GET | Company name, logo, API version, app on/off (for the sign-in screen) |
| `login` (`usr`, `pwd`) | POST | Checks the password and returns `token` and the employee profile. Rate limited to 10 tries per 10 minutes. |
| `sign_out_all_devices` | POST | Replaces the API secret, signing the user out on every phone |
| `profile` | GET | Employee details, company currency, today's shift |
| `checkin_status` | GET | Today's check-ins, next action (IN/OUT), shift, whether GPS is required |
| `checkin` (`log_type`, `latitude`, `longitude`, `accuracy`) | POST | Records an Employee Checkin at the server's time |
| `attendance` (`month` = YYYY-MM) | GET | Attendance days, holidays and totals for the month |
| `leave_balances` | GET | Allocated, taken, pending and remaining days per leave type |
| `leave_applications` | GET | The employee's leave requests |
| `apply_leave` (`leave_type`, `from_date`, `to_date`, `half_day`, `half_day_date`, `reason`) | POST | Creates an open Leave Application for the leave approver |
| `salary_slips` | GET | Submitted salary slips |
| `salary_slip_pdf` (`name`) | GET | The salary slip as a PDF |
| `expense_claim_types` | GET | Expense types to choose from |
| `expense_claims` | GET | The employee's expense claims |
| `submit_expense_claim` (`expense_type`, `amount`, `expense_date`, `description`, `receipt`, `receipt_filename`) | POST | Creates a draft Expense Claim for the expense approver, with an optional base64 receipt photo (max 5 MB, saved as a private file) |

Errors use Frappe's normal responses: an HTTP error status with the message in `_server_messages`.

The token is the user's ERPNext API key and secret. It is created at first login and reused on
later logins, so an employee can use more than one phone; `sign_out_all_devices` revokes it.

#### License

mit
