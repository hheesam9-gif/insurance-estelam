import argparse
import json
import base64
import os
import sys
import jdatetime
from playwright.sync_api import sync_playwright

BASE_URL = "https://apps.mellatinsurance.ir/Automobile2010/Forms/frmEstelamSanhab.aspx"
LOSS_DEDUCTION_PERCENT = 20
LETTER_MAP = {"ی": "ي", "ک": "ك"}


def fetch_result_html(region, three_digit, letter, left_two_digit):
    letter = letter.translate(str.maketrans(LETTER_MAP))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        page.select_option("#CentralContentPlaceHolder_ddlInsType", "1")
        page.wait_for_timeout(1000)
        page.select_option("#CentralContentPlaceHolder_ddlInqueryItemCode", "5")

        page.wait_for_selector(
            "#CentralContentPlaceHolder_txtPelakSrl", state="visible", timeout=15000
        )

        page.fill("#CentralContentPlaceHolder_txtPelakSrl", region)
        page.fill("#CentralContentPlaceHolder_txtPelak3", three_digit)
        page.select_option("#CentralContentPlaceHolder_ddlPelak2", letter)
        page.fill("#CentralContentPlaceHolder_txtPelak1", left_two_digit)

        page.click("#CentralContentPlaceHolder_btnInquery")
        page.wait_for_load_state("networkidle")

        html = page.content()
        browser.close()
        return html


def extract_policy_json(html):
    idx = html.find('name="__VIEWSTATE"')
    if idx == -1:
        return None
    start_val = html.find('value="', idx) + len('value="')
    end_val = html.find('"', start_val)
    viewstate_b64 = html[start_val:end_val]

    try:
        raw = base64.b64decode(viewstate_b64)
    except Exception:
        return None

    text = raw.decode("utf-8", errors="ignore")
    start = text.find('{"Error"')
    if start == -1:
        return None

    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
        return obj
    except json.JSONDecodeError:
        return None


def parse_jalali_date(date_str):
    if not date_str:
        return None
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        return None
    y, m, d = parts
    try:
        return jdatetime.date(int(y), int(m), int(d))
    except ValueError:
        return None


def fmt_date(d):
    if d is None:
        return "-"
    return f"{d.year:04d}/{d.month:02d}/{d.day:02d}"


def to_number(value):
    if value in (None, "", "-"):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_policies(data):
    if not data or "Policy" not in data:
        return []

    policies = []
    for item in data["Policy"]:
        if "ثالث" not in (item.get("TypPlcy") or ""):
            continue

        start = parse_jalali_date(item.get("HBgnDte"))
        end = parse_jalali_date(item.get("HEndDte"))
        if not start or not end:
            continue

        policies.append({
            "unique_code": item.get("PlcyUnqCod"),
            "company": item.get("CmpNam"),
            "insured_name": item.get("InsNam"),
            "plate": item.get("Plk"),
            "chassis": item.get("ShsNum"),
            "engine": item.get("MtrNum"),
            "vin": item.get("VIN"),
            "start_date": start,
            "end_date": end,
            "financial_percent": to_number(item.get("DisFnYrPrcnt")),
            "bodily_percent": to_number(item.get("DisLfYrPrcnt")),
            "passenger_percent": to_number(item.get("DisPrsnYrPrcnt")),
            "losses": item.get("Losses") or [],
        })

    policies.sort(key=lambda p: p["start_date"])
    return policies


def vehicle_key(p):
    return (p["chassis"], p["engine"])


def compute_one_field(policies, field_name):
    known = [p for p in policies if p[field_name] is not None]
    if not known:
        return 0, []

    known.sort(key=lambda p: p["start_date"])
    warnings = []

    for i in range(1, len(known)):
        prev, cur = known[i - 1], known[i]
        if vehicle_key(prev) != vehicle_key(cur):
            continue

        gap_days = (cur["start_date"] - prev["end_date"]).days
        prev_duration = (prev["end_date"] - prev["start_date"]).days
        continuous_full_year = gap_days <= 1 and prev_duration >= 360

        expected = min(prev[field_name] + 5, 70) if continuous_full_year else prev[field_name]
        if cur[field_name] not in (expected, prev[field_name]):
            warnings.append(
                f"مغایرت {field_name} در {cur['unique_code']}: "
                f"انتظار {expected}، ثبت‌شده {cur[field_name]}"
            )

    return known[-1][field_name], warnings


def loss_category(loss_type):
    t = (loss_type or "").strip()
    if "مالی" in t:
        return "financial"
    if "جانی" in t or "فوت" in t or "نقص عضو" in t:
        return "bodily"
    if "سرنشین" in t:
        return "passenger"
    return None


def dedupe_losses(raw_losses):
    events = {}
    for loss in raw_losses or []:
        doc_no = (loss.get("LosCmpDocNo") or "").strip()
        loss_type = (loss.get("LosTyp") or "").strip()
        ann_date = loss.get("HAncDte")
        key = doc_no or f"{loss_type}|{ann_date}|{loss.get('PayAmnt')}"
        if key not in events:
            events[key] = loss_type
    return events


def apply_loss_deduction(value, event_count):
    if value is None or event_count <= 0:
        return value
    return max(0, value - LOSS_DEDUCTION_PERCENT * event_count)


def compute_discount(policies):
    if not policies:
        return {"financial": 0, "bodily": 0, "passenger": 0, "warnings": []}

    fn, w1 = compute_one_field(policies, "financial_percent")
    lf, w2 = compute_one_field(policies, "bodily_percent")
    pr, w3 = compute_one_field(policies, "passenger_percent")
    warnings = w1 + w2 + w3

    last_policy = policies[-1]
    events = dedupe_losses(last_policy.get("losses"))

    counts = {"financial": 0, "bodily": 0, "passenger": 0}
    for loss_type in events.values():
        cat = loss_category(loss_type)
        if cat:
            counts[cat] += 1
        else:
            warnings.append(f"نوع خسارت ناشناخته در آخرین بیمه‌نامه: '{loss_type}'")

    fn = apply_loss_deduction(fn, counts["financial"])
    lf = apply_loss_deduction(lf, counts["bodily"])
    pr = apply_loss_deduction(pr, counts["passenger"])

    if counts["financial"]:
        warnings.append(
            f"کسر بابت خسارت مالی آخرین بیمه‌نامه ({last_policy['unique_code']}): "
            f"{counts['financial']} فقره × {LOSS_DEDUCTION_PERCENT}٪"
        )
    if counts["bodily"]:
        warnings.append(
            f"کسر بابت خسارت جانی آخرین بیمه‌نامه ({last_policy['unique_code']}): "
            f"{counts['bodily']} فقره × {LOSS_DEDUCTION_PERCENT}٪"
        )
    if counts["passenger"]:
        warnings.append(
            f"کسر بابت خسارت سرنشین آخرین بیمه‌نامه ({last_policy['unique_code']}): "
            f"{counts['passenger']} فقره × {LOSS_DEDUCTION_PERCENT}٪"
        )

    return {"financial": fn, "bodily": lf, "passenger": pr, "warnings": warnings}


def write_summary(region, three_digit, letter, left_two_digit, result, policies):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    plate = f"ایران {left_two_digit} - {three_digit} {letter} {region}"
    lines = []
    lines.append(f"## نتیجه استعلام پلاک {plate}")
    lines.append("")
    lines.append("| نوع تخفیف | درصد |")
    lines.append("|---|---|")
    lines.append(f"| مالی | {result['financial']}% |")
    lines.append(f"| جانی | {result['bodily']}% |")
    lines.append(f"| سرنشین | {result['passenger']}% |")
    lines.append("")

    if policies:
        lines.append("### بیمه‌نامه‌های ثالث پیدا‌شده")
        lines.append("")
        lines.append("| کد | شروع | پایان | مالی | جانی | سرنشین |")
        lines.append("|---|---|---|---|---|---|")
        for p in policies:
            lines.append(
                f"| {p['unique_code']} | {fmt_date(p['start_date'])} | {fmt_date(p['end_date'])} | "
                f"{p['financial_percent']} | {p['bodily_percent']} | {p['passenger_percent']} |"
            )
        lines.append("")

    if result["warnings"]:
        lines.append("### هشدارها")
        for w in result["warnings"]:
            lines.append(f"- ⚠ {w}")

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--three-digit", required=True)
    parser.add_argument("--letter", required=True)
    parser.add_argument("--left-two-digit", required=True)
    args = parser.parse_args()

    print("در حال استعلام...")
    html = fetch_result_html(args.region, args.three_digit, args.letter, args.left_two_digit)

    data = extract_policy_json(html)
    if not data:
        print("داده‌ای پیدا نشد.")
        sys.exit(1)

    policies = parse_policies(data)
    if not policies:
        print("هیچ بیمه‌نامه ثالثی پیدا نشد.")
        sys.exit(1)

    print("\n--- بیمه‌نامه‌ها ---")
    for p in policies:
        print(
            f"{p['unique_code']}  |  شروع={fmt_date(p['start_date'])}  "
            f"پایان={fmt_date(p['end_date'])}  |  "
            f"مالی={p['financial_percent']}  جانی={p['bodily_percent']}  "
            f"سرنشین={p['passenger_percent']}"
        )

    result = compute_discount(policies)

    print("\n--- نتیجه ---")
    print(f"درصد قابل انتقال مالی: {result['financial']}")
    print(f"درصد قابل انتقال جانی: {result['bodily']}")
    print(f"درصد قابل انتقال سرنشین: {result['passenger']}")

    if result["warnings"]:
        print("\n--- هشدارها ---")
        for w in result["warnings"]:
            print("- " + w)

    write_summary(args.region, args.three_digit, args.letter, args.left_two_digit, result, policies)


if __name__ == "__main__":
    main()
