import pytest
from socket import AddressFamily

from happyeyeballs import interleave_family


@pytest.mark.parametrize(
    ("input", "output"),
    [
        ([(AddressFamily.AF_INET, 1)], [(AddressFamily.AF_INET, 1)]),
        (
            [(AddressFamily.AF_INET, 1), (AddressFamily.AF_INET6, 2)],
            [(AddressFamily.AF_INET, 1), (AddressFamily.AF_INET6, 2)],
        ),
        (
            [(AddressFamily.AF_INET, 1), (AddressFamily.AF_INET, 3), (AddressFamily.AF_INET6, 2)],
            [(AddressFamily.AF_INET, 1), (AddressFamily.AF_INET6, 2), (AddressFamily.AF_INET, 3)],
        ),
    ],
)
def test_interleave(input, output):
    result = list(interleave_family(input))
    assert result == output
