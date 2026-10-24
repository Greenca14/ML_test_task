import argparse
import datetime as dt
import json
import re
import sys


FIELDS = (
    "Last_name_ru",
    "First_name_ru",
    "Middle_name_ru",
    "Sex_ru",
    "Birth_date",
    "Birth_place_ru",
    "Issue_date",
    "Issue_organisation_code",
    "Licence_number",
    "Issue_organization_ru",
)
AS_OF = dt.date(2026, 10, 1)
CONFIDENCE_MARGIN = 0.001

THRESHOLDS = {
    "web": (0.9975, 0.90, 0.99, 0.97, 0.99, 0.97, 0.985, 0.985, 0.97, 0.995),
    "mobile": (0.99, 0.90, 0.95, 0.995, 0.9925, 0.99, 0.90, 0.99, 0.90, 0.9925),
}


def field_text_and_min_conf(field):
    if not isinstance(field, dict):
        return "", 0.0
    lines = field.get("lines")
    if not isinstance(lines, list):
        return "", 0.0
    text_parts = []
    confidences = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if isinstance(text, str):
            text_parts.append(text)
        char_conf = line.get("char_conf")
        if isinstance(char_conf, list):
            for confidence in char_conf:
                if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
                    confidences.append(float(confidence))
                else:
                    return " ".join(text_parts).strip(), 0.0
    return " ".join(" ".join(text_parts).split()), min(confidences, default=0.0)


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        return None
    try:
        return dt.datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


def add_years(value, years):
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def passport_valid_on(birth_date, issue_date):
    if birth_date is None or issue_date is None or issue_date > AS_OF:
        return False
    if birth_date >= issue_date:
        return False

    age_at_issue = issue_date.year - birth_date.year
    if (issue_date.month, issue_date.day) < (birth_date.month, birth_date.day):
        age_at_issue -= 1
    if age_at_issue < 14:
        return False
    if age_at_issue >= 45:
        return True

    expiry_age = 20 if age_at_issue < 20 else 45
    return AS_OF < add_years(birth_date, expiry_age)


def mrz_check_digit(value):
    weights = (7, 3, 1)
    total = 0
    for index, char in enumerate(value):
        if char.isdigit():
            number = int(char)
        elif "A" <= char <= "Z":
            number = ord(char) - ord("A") + 10
        elif char == "<":
            number = 0
        else:
            return None
        total += number * weights[index % 3]
    return total % 10


def mrz_date(value):
    if len(value) != 6 or not value.isdigit():
        return ""
    year = int(value[:2])
    full_year = 2000 + year if year <= AS_OF.year % 100 else 1900 + year
    return f"{full_year:04d}{value[2:4]}{value[4:6]}"


def date_digits(value):
    parsed = parse_date(value)
    if parsed is None:
        return ""
    return f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"


def valid_mrz_and_crosschecks(prediction, fields):
    mrz = prediction.get("mrz")
    lines = mrz.get("lines") if isinstance(mrz, dict) else None
    if not isinstance(lines, list) or len(lines) < 2:
        return False
    first = lines[0].get("text", "") if isinstance(lines[0], dict) else ""
    second = lines[1].get("text", "") if isinstance(lines[1], dict) else ""
    if len(first) != 44 or len(second) != 44:
        return False

    checks = (
        (second[:9], second[9]),
        (second[13:19], second[19]),
        (second[28:42], second[42]),
        (second[:10] + second[13:20] + second[21:43], second[43]),
    )
    if any(not digit.isdigit() or mrz_check_digit(value) != int(digit) for value, digit in checks):
        return False

    serial_digits = "".join(char for char in fields["Licence_number"] if char.isdigit())
    code_digits = "".join(char for char in fields["Issue_organisation_code"] if char.isdigit())
    if len(serial_digits) != 10 or len(code_digits) != 6:
        return False
    if second[:2] + second[3:9] != serial_digits[:2] + serial_digits[-6:]:
        return False
    if second[13:19] != date_digits(fields["Birth_date"])[2:]:
        return False
    if second[29:35] != date_digits(fields["Issue_date"])[2:]:
        return False
    if second[35:41] != code_digits:
        return False

    sex = fields["Sex_ru"].strip().upper()
    expected_sex = "M" if sex in ("МУЖ", "МУЖ.") else "F" if sex in ("ЖЕН", "ЖЕН.") else ""
    return bool(expected_sex) and second[20] == expected_sex


def make_decision(prediction):
    source = prediction.get("source")
    thresholds = THRESHOLDS.get(source)
    if thresholds is None:
        return {"doc_id": prediction["doc_id"], "decision": "reject"}

    viz = prediction.get("viz")
    if not isinstance(viz, dict):
        return {"doc_id": prediction["doc_id"], "decision": "reject"}

    fields = {}
    for name, threshold in zip(FIELDS, thresholds):
        value, confidence = field_text_and_min_conf(viz.get(name))
        if not value or confidence < min(1.0, threshold + CONFIDENCE_MARGIN):
            return {"doc_id": prediction["doc_id"], "decision": "reject"}
        fields[name] = value

    fields["Issue_organisation_code"] = re.sub(
        r"^(\d{3})[- ]?(\d{3})$", r"\1-\2", fields["Issue_organisation_code"]
    )
    fields["Licence_number"] = " ".join(fields["Licence_number"].split())

    if not re.fullmatch(r"\d{3}-\d{3}", fields["Issue_organisation_code"]):
        return {"doc_id": prediction["doc_id"], "decision": "reject"}
    if not re.fullmatch(r"\d{2}\s+\d{2}\s+\d{6}", fields["Licence_number"]):
        return {"doc_id": prediction["doc_id"], "decision": "reject"}
    if fields["Sex_ru"].strip().upper() not in ("МУЖ", "МУЖ.", "ЖЕН", "ЖЕН."):
        return {"doc_id": prediction["doc_id"], "decision": "reject"}

    birth_date = parse_date(fields["Birth_date"])
    issue_date = parse_date(fields["Issue_date"])
    if not passport_valid_on(birth_date, issue_date):
        return {"doc_id": prediction["doc_id"], "decision": "reject"}
    if not valid_mrz_and_crosschecks(prediction, fields):
        return {"doc_id": prediction["doc_id"], "decision": "reject"}

    return {"doc_id": prediction["doc_id"], "decision": "accept", "fields": fields}


def process(input_path, output_path):
    seen_ids = set()
    with open(input_path, encoding="utf-8") as source, open(
        output_path, "w", encoding="utf-8", newline="\n"
    ) as destination:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"Empty input line at {line_number}")
            try:
                prediction = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON at line {line_number}: {error}") from error
            if not isinstance(prediction, dict) or not isinstance(prediction.get("doc_id"), str):
                raise ValueError(f"Missing string doc_id at line {line_number}")
            if prediction["doc_id"] in seen_ids:
                raise ValueError(f"Duplicate doc_id at line {line_number}: {prediction['doc_id']}")
            seen_ids.add(prediction["doc_id"])
            destination.write(json.dumps(make_decision(prediction), ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_jsonl")
    parser.add_argument("output_jsonl")
    args = parser.parse_args()
    try:
        process(args.input_jsonl, args.output_jsonl)
    except (OSError, ValueError) as error:
        print(f"postprocess.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
