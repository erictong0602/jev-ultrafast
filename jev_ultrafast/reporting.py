"""Post-run verification and structured records. A model judgment gets checked against evidence."""


def check_expectations(page, expect_url=None, expect_text=None):
    """Substring checks over the last observed page. Only requested checks appear in the result."""
    page = page or {}
    checks = {}
    if expect_url:
        checks["url"] = expect_url in page.get("url", "")
    if expect_text:
        checks["text"] = expect_text in page.get("text", "")
    return checks


def build_record(*, url, status, detail="", page=None, actions=0, decisions=0, elapsed_ms=0,
                 errors=None, expectations=None):
    """One JSON-serializable outcome row: the judgment, its evidence, and any expectation results."""
    return {
        "url": url,
        "status": status,
        "detail": detail,
        "final_url": (page or {}).get("url"),
        "actions": actions,
        "decisions": decisions,
        "elapsed_ms": elapsed_ms,
        "errors": errors or {"counts": {}, "items": []},
        "expectations": expectations,
    }
