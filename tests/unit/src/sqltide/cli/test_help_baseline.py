import hashlib

import pytest
from _pytest.capture import CaptureFixture

from sqltide._native import parse_cli_arguments
from tests.unit.src.sqltide.cli._test_types import CliHelpBaselineTestCase


@pytest.mark.parametrize(
    "test_case",
    [
        CliHelpBaselineTestCase(
            description="captures root help",
            argv=("--help",),
            expected_sha256="0f958a0704f3fcd2e684d9ac738c547d0785cc4be3aefe90d8291306b78de0ae",
        ),
        CliHelpBaselineTestCase(
            description="captures admin help",
            argv=("admin", "--help"),
            expected_sha256="8a71117b230ad9c2a0920d0c264609cd1d5267da2945f3421206b73e00ebb46d",
        ),
        CliHelpBaselineTestCase(
            description="captures dev help",
            argv=("dev", "--help"),
            expected_sha256="1fbb6dcf6d60feb07172716a967c5b76af3eb337ac5d119bada0417272e2a465",
        ),
        CliHelpBaselineTestCase(
            description="captures discover help",
            argv=("discover", "--help"),
            expected_sha256="fc62285870658e3c07929e18a8661a97167273d634fe3b78a5ce1d19ea2ea764",
        ),
        CliHelpBaselineTestCase(
            description="captures compile help",
            argv=("compile", "--help"),
            expected_sha256="e0737d4aaf5de69b276d1139ad16d74d24cf2e5ba4bc2ccf6cf6267dde28c320",
        ),
        CliHelpBaselineTestCase(
            description="captures test help",
            argv=("test", "--help"),
            expected_sha256="355b056a852094825b244bf125de32f50affac1450c5ebace5c8c2b0a0b8140c",
        ),
        CliHelpBaselineTestCase(
            description="captures plan help",
            argv=("plan", "--help"),
            expected_sha256="5e61de1f6441e62ce617db135a451ba8d145690d2725917997ead771b83efed8",
        ),
        CliHelpBaselineTestCase(
            description="captures build help",
            argv=("build", "--help"),
            expected_sha256="f367770c7abf28553ba7e099bebc305071cb6296e3e64109e42fbc47a8d8a715",
        ),
        CliHelpBaselineTestCase(
            description="captures destroy help",
            argv=("destroy", "--help"),
            expected_sha256="fe2be29e7b30d73cc3e228e73b834dd4c60bdef6d9187cdf497299754a880003",
        ),
        CliHelpBaselineTestCase(
            description="captures reset target help",
            argv=("reset-target", "--help"),
            expected_sha256="8f34601d874163d6a4ea86aab6ba7243af063c2e89d92d461ffe6189b0ba7b7f",
        ),
        CliHelpBaselineTestCase(
            description="captures audit help",
            argv=("audit", "--help"),
            expected_sha256="b247abe760fa42b77a720eee195372fb49a4cf8aea0dac47f18f9c5e1990722e",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment help",
            argv=("deployment", "--help"),
            expected_sha256="0fd919a9d6e2daaacea62602777b3467673b0b76ca8dde40416b85ed1dc58b4f",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment list help",
            argv=("deployment", "list", "--help"),
            expected_sha256="57c2b724a171c0e7a75cb9c1a0ac0a23784fba5998b003a5a6381c9ee80d864c",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment show help",
            argv=("deployment", "show", "--help"),
            expected_sha256="d89034d83ae58eaf099a627cb57714593bccc71afa56ea153ff2d96150150b6d",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment audit help",
            argv=("deployment", "audit", "--help"),
            expected_sha256="c983dc5b8a6518a07e9a6c39f88c9ea8ff45170992d4a518aadf750c3a023559",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment promote help",
            argv=("deployment", "promote", "--help"),
            expected_sha256="67b1a4f34878eb64992cb1bf9f9c9802bccd5244ec7ce8b862ee94e7b9c4d1e2",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment diff help",
            argv=("deployment", "diff", "--help"),
            expected_sha256="a55cb97fcdc0c0bff5d3ef9580cc699877bb5aee8bc8f485070c3de7173ea576",
        ),
        CliHelpBaselineTestCase(
            description="captures deployment rollback help",
            argv=("deployment", "rollback", "--help"),
            expected_sha256="cda5976ebc45f2711b82c04a906960c70607e4d88a509650b38e24d7d7461f1b",
        ),
        CliHelpBaselineTestCase(
            description="captures reconcile help",
            argv=("reconcile", "--help"),
            expected_sha256="fc01e63a3e0a2dff1d689e074284bacae7d43d68fa2043bd6c32ca3f3afb9652",
        ),
        CliHelpBaselineTestCase(
            description="captures rename-project help",
            argv=("rename-project", "--help"),
            expected_sha256="e5ed515f1e543aa5a750757a63811df4e532d6bc90c21d6ea7e50da214d80d84",
        ),
        CliHelpBaselineTestCase(
            description="captures janitor help",
            argv=("janitor", "--help"),
            expected_sha256="04e582aeabaca4a8f954c7aa2411d1c173fb6216cb7498a26bf44209731f3fb6",
        ),
        CliHelpBaselineTestCase(
            description="captures doctor help",
            argv=("doctor", "--help"),
            expected_sha256="a1c647313e2d4c134a4c965612d99351aaf87855ea2051cf3680325449d6e82b",
        ),
        CliHelpBaselineTestCase(
            description="captures repair help",
            argv=("repair", "--help"),
            expected_sha256="bb206d55a81b9b52be02307f16b233c74fe2c88650cff7f3394f1c7f92ee6192",
        ),
        CliHelpBaselineTestCase(
            description="captures repair active-view help",
            argv=("repair", "active-view", "--help"),
            expected_sha256="025985f98996c955b07caa9106890ae17310dc4a3c59370d69bb8c481b2bbd2f",
        ),
        CliHelpBaselineTestCase(
            description="captures lint help",
            argv=("lint", "--help"),
            expected_sha256="e81f91cc51cd29af010e245fc0a72aaad46ddbc4943a2d2e3b8049524a55a967",
        ),
        CliHelpBaselineTestCase(
            description="captures format help",
            argv=("format", "--help"),
            expected_sha256="76b213bb0b69bc73038610e0bf50682cf706c7283d8bf292476bf198881c7edc",
        ),
    ],
    ids=lambda case: case.description,
)
def test_given_slice_zero_command_when_rendering_help_then_matches_baseline(
    test_case: CliHelpBaselineTestCase,
    capsys: CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        parse_cli_arguments(list(test_case.argv))
    captured_output: str = capsys.readouterr().out
    actual_sha256: str = hashlib.sha256(captured_output.encode("utf-8")).hexdigest()

    assert actual_sha256 == test_case.expected_sha256
