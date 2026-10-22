from pathlib import Path

def test_handler_exists():
    assert (Path(__file__).resolve().parents[1] / "handler.py").is_file()
