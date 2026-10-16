import re
from typing import Annotated
from pydantic import AfterValidator


def validate_phone(value: str) -> str:
    value = value.strip()
    if len(value) > 50 or not re.fullmatch(r"\+?[0-9 ()-]+", value) or not 7 <= len(re.sub(r"\D", "", value)) <= 15:
        raise ValueError("Enter a phone number with 7–15 digits, optionally including +, spaces, parentheses or hyphens")
    return value

PhoneNumber = Annotated[str, AfterValidator(validate_phone)]
