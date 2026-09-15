from launcher import errors


def test_diagnoses_missing_pypi_package():
    log = "ERROR: No matching distribution found for fastapi-utilz\n"
    d = errors.diagnose(log)
    assert "fastapi-utilz" in d.summary
    assert "does not exist" in d.summary


def test_diagnoses_missing_import():
    log = "ModuleNotFoundError: No module named 'sqlmodel'\n"
    d = errors.diagnose(log)
    assert "sqlmodel" in d.summary
    assert "requirements.txt" in d.summary


def test_diagnoses_missing_build_script():
    d = errors.diagnose('npm ERR! missing script: "build"')
    assert "no \"build\" command" in d.summary


def test_diagnoses_out_of_memory():
    d = errors.diagnose("The command returned a non-zero code: 137")
    assert "out of memory" in d.summary


def test_unknown_failure_still_gives_guidance():
    d = errors.diagnose("something entirely unexpected")
    assert "Copy error for AI" in (d.hint or "")


def test_detects_localhost_in_frontend_source():
    d = errors.check_localhost_leak('const API = "http://localhost:8000/api";')
    assert d is not None
    assert "8000" in d.summary
    assert "/api" in (d.hint or "")


def test_relative_api_calls_are_not_flagged():
    assert errors.check_localhost_leak('fetch("/api/items")') is None


def test_repair_prompt_is_self_contained():
    d = errors.diagnose("ModuleNotFoundError: No module named 'sqlmodel'")
    prompt = errors.repair_prompt("sales-dashboard", d, "line one\nline two\n")
    assert "sales-dashboard" in prompt
    assert "sqlmodel" in prompt
    assert "line two" in prompt
    assert "corrected ZIP" in prompt


def test_log_tail_drops_blank_noise():
    assert errors.log_tail("a\n\n\nb\n", lines=2) == "a\nb"


def test_docker_outage_is_blamed_on_the_server_not_the_upload():
    log = ("ERROR: failed to connect to the docker API at "
           "unix:///var/run/docker.sock")
    d = errors.diagnose(log)
    assert "not running" in d.summary
    assert "not with your ZIP" in (d.hint or "")


def test_a_missing_windows_time_zone_database_is_explained():
    """zoneinfo reads the system IANA database, which Windows does not have.
    An app written on a Mac dies at import on the server, before it binds its
    port, and the traceback names tzdata only as a second ModuleNotFoundError
    further up - easy to read past."""
    log = (
        '  File "backend\\\\refresh_pipeline.py", line 31, in <module>\n'
        '    IST = ZoneInfo("Asia/Kolkata")\n'
        "zoneinfo._common.ZoneInfoNotFoundError: "
        "'No time zone found with key Asia/Kolkata'\n"
    )
    result = errors.diagnose(log)

    assert "Asia/Kolkata" in result.summary
    assert "tzdata" in (result.hint or ""), "the cure has to be named"
