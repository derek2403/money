from __future__ import annotations

import re

NAME = r"[A-Za-z][A-Za-z0-9_]*"
AMOUNT = r"(?P<amount>\d+(?:\.\d+)?)"
SEP = r"(?:\s*,\s*|\s+and\s+)"

PATTERN_EVERYONE = re.compile(
    rf"^\s*everyone\s+(?:to|gives?)\s+(?P<payer>{NAME})\s+{AMOUNT}\s*$",
    re.IGNORECASE,
)
PATTERN_MULTI = re.compile(
    rf"^\s*(?P<debtors>{NAME}(?:{SEP}{NAME})+)\s+(?:to|gives?)\s+(?P<payer>{NAME})\s+{AMOUNT}\s*$",
    re.IGNORECASE,
)
PATTERN_SINGLE = re.compile(
    rf"^\s*(?P<debtor>{NAME})\s+(?:to|gives?)\s+(?P<payer>{NAME})\s+{AMOUNT}\s*$",
    re.IGNORECASE,
)


def parse_entry(text: str, joiners: list[str]) -> dict | None:
    """
    Parse a debt-recording message.

    Returns one of:
      - {"payer": str, "debtors": [str, ...], "amount_per_debtor": float}
      - {"error": str}  -- the structure looked right but a name didn't match
      - None            -- not a recognized debt message; ignore silently
    """
    joiner_map = {j.lower(): j for j in joiners}

    def resolve(raw: str) -> str | None:
        return joiner_map.get(raw.lower())

    def err_for(*raws: str) -> dict | None:
        unknown = [r for r in raws if resolve(r) is None]
        if unknown:
            return {
                "error": (
                    f"Unknown name(s): {', '.join(unknown)}. "
                    f"Joiners are: {', '.join(joiners)}."
                )
            }
        return None

    m = PATTERN_EVERYONE.match(text)
    if m:
        e = err_for(m.group("payer"))
        if e:
            return e
        payer = resolve(m.group("payer"))
        debtors = [j for j in joiners if j != payer]
        if not debtors:
            return {"error": "No other joiners to split with."}
        return {
            "payer": payer,
            "debtors": debtors,
            "amount_per_debtor": float(m.group("amount")),
        }

    m = PATTERN_MULTI.match(text)
    if m:
        raw_debtors = re.split(SEP, m.group("debtors"))
        e = err_for(m.group("payer"), *raw_debtors)
        if e:
            return e
        payer = resolve(m.group("payer"))
        debtors = [resolve(n) for n in raw_debtors]
        if payer in debtors:
            return {"error": f"{payer} can't owe themselves."}
        return {
            "payer": payer,
            "debtors": debtors,
            "amount_per_debtor": float(m.group("amount")),
        }

    m = PATTERN_SINGLE.match(text)
    if m:
        raw_debtor = m.group("debtor")
        raw_payer = m.group("payer")
        # Avoid hijacking real chat: only treat as a debt entry if at least one
        # of the two names is a known joiner.
        if resolve(raw_debtor) is None and resolve(raw_payer) is None:
            return None
        e = err_for(raw_debtor, raw_payer)
        if e:
            return e
        payer = resolve(raw_payer)
        debtor = resolve(raw_debtor)
        if payer == debtor:
            return {"error": f"{payer} can't owe themselves."}
        return {
            "payer": payer,
            "debtors": [debtor],
            "amount_per_debtor": float(m.group("amount")),
        }

    return None
