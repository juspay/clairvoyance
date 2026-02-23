def indian_number_to_speech(number: int) -> str:
    if number <= 0:
        return "0 rupees"
    parts = []
    crore = number // 10_000_000
    number %= 10_000_000
    lakh = number // 100_000
    number %= 100_000
    thousand = number // 1_000
    number %= 1_000
    hundred = number // 100
    number %= 100
    if crore:
        parts.append(f"{crore} crore")
    if lakh:
        parts.append(f"{lakh} lakh")
    if thousand:
        parts.append(f"{thousand} thousand")
    if hundred:
        parts.append(f"{hundred} hundred")
    if number:
        parts.append(str(number))
    return " ".join(parts) + " rupees"
