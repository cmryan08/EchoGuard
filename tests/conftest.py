import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def classic_fixture() -> Path:
    return FIXTURES / "classic_esp32s3_sample.txt"


@pytest.fixture
def c5c6_fixture() -> Path:
    return FIXTURES / "c5c6_sample.txt"


@pytest.fixture
def classic_doc_line(classic_fixture) -> str:
    """The example line from the official esp-csi get-started README."""
    for line in classic_fixture.read_text().splitlines():
        if line.startswith("CSI_DATA,0,94:d9:b3:80:8c:81"):
            return line
    raise AssertionError("documented example line missing from fixture")


@pytest.fixture
def c5c6_doc_line(c5c6_fixture) -> str:
    for line in c5c6_fixture.read_text().splitlines():
        if line.startswith("CSI_DATA,7,1a:00:00:00:00:00,-23,11,-96,32,4,11,372852,"):
            return line
    raise AssertionError("documented C5/C6 example line missing from fixture")
