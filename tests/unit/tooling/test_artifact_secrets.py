"""The artifact secret gate, proved by making it fail.

The repository's ordinary scan ignores ``build/`` -- it has to, because the artifact build
unpacks third-party wheels whose *documentation* is credential-shaped. That exemption is fine
for the repository scan and would be catastrophic as the answer for the artifacts themselves: a
zip must never be publishable because the directory it was written into is not scanned.

So the generated artifacts get their own gate, and every assertion below is about it refusing
something. A check that has only ever been seen to pass is a check nobody has tested.

Two properties matter most and each has its own test:

* **A first-party file gets no exception, ever.** The same byte sequence that is waved through
  in ``botocore``'s example documents fails the build in ``src/chorus``.
* **The gate reads the final zip, not the staging tree.** A secret introduced after staging --
  by a builder bug, or by anything that touched the archive -- is still caught.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from tools import build_runtime_artifacts as build
from tools.check_secrets import PATTERNS

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

_TOKEN_FIELD = "api" + "_key"
_SECRET_FIELD = "aws_secret" + "_access_key"
_PEM_LABEL = "RSA PRIVATE" + " KEY"

FAKE_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"[:16]
FAKE_TOKEN_ASSIGNMENT = f'{_TOKEN_FIELD} = "not-a-real-value-abcdef123456"'
FAKE_SECRET_ASSIGNMENT = f"{_SECRET_FIELD} = '{FAKE_ACCESS_KEY}'"
FAKE_ACCESS_KEY_ASSIGNMENT = f"KEY = '{FAKE_ACCESS_KEY}'"
FAKE_PRIVATE_KEY = f"-----BEGIN {_PEM_LABEL}-----\nnot-a-real-key\n-----END {_PEM_LABEL}-----\n"
"""Synthetic values, deliberately shaped rather than valid, and **assembled at run time**.

They have to match the repository's own patterns -- a negative test whose input matches nothing
proves the input, not the gate -- which means writing them as literals would make this file trip
``tools/check_secrets.py`` on every run. Concatenating the field names keeps the source text
clean while the values these tests actually feed the scanner are exactly the shapes it hunts.
Weakening the scanner to accommodate a fixture would have been the wrong trade.
"""


def test_the_gate_uses_the_repositorys_own_patterns_rather_than_a_second_copy() -> None:
    """One definition of what a secret looks like, applied in two places."""

    assert build.SECRET_PATTERNS is PATTERNS


def test_the_synthetic_values_actually_match_something() -> None:
    """A negative test whose input matches no pattern proves the input, not the gate."""

    assert build.secret_content_problem("main.py", FAKE_ACCESS_KEY.encode(), first_party=True)
    assert build.secret_content_problem("main.py", FAKE_TOKEN_ASSIGNMENT.encode(), first_party=True)
    assert build.secret_content_problem("main.py", FAKE_PRIVATE_KEY.encode(), first_party=True)


# -- A: first-party content, no exceptions ------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        pytest.param("main.py", FAKE_ACCESS_KEY, id="access-key-in-root-entrypoint"),
        pytest.param("src/chorus/contracts/common.py", FAKE_TOKEN_ASSIGNMENT, id="token-in-chorus"),
        pytest.param("runtimes/server.py", FAKE_PRIVATE_KEY, id="private-key-in-runtimes"),
    ],
)
def test_a_secret_in_a_first_party_file_fails_the_staging_scan(
    tmp_path: Path, relative: str, content: str
) -> None:
    """The first gate, reported against the file a person can open."""

    staged = tmp_path / "staging"
    path = staged / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# a comment\n{content}\n", encoding="utf-8")

    problems = build.scan_staged_first_party(staged, [relative])

    assert len(problems) == 1
    assert relative in problems[0]
    assert content not in problems[0], "a finding must never quote the value it found"


def test_a_secret_in_a_first_party_file_fails_the_final_archive_scan(tmp_path: Path) -> None:
    """The second gate, over the bytes that would actually be published."""

    archive_path = tmp_path / "leaky.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", f"SECRET = '{FAKE_ACCESS_KEY}'\n")
        archive.writestr("runtimes/__init__.py", "")

    problems = build.inspect_archive(archive_path, first_party=frozenset({"main.py"}))

    assert any("credential-shaped content" in problem for problem in problems)
    assert any("main.py" in problem for problem in problems)


def test_a_first_party_path_is_never_exempted_by_the_vendor_list(tmp_path: Path) -> None:
    """The property the whole exception mechanism turns on.

    The same content the vendor list waves through in ``botocore``'s example document fails when
    it is in a file this build copied out of the repository -- and it fails even if somebody
    adds that repository path to the exception set, because the set is not consulted at all for
    a first-party entry.
    """

    exempted = next(iter(build.VENDOR_CONTENT_EXCEPTIONS))
    payload = FAKE_SECRET_ASSIGNMENT.encode()

    assert build.secret_content_problem(exempted, payload, first_party=False) is None
    assert build.secret_content_problem(exempted, payload, first_party=True) is not None
    assert build.secret_content_problem("src/chorus/settings.py", payload, first_party=True)


def test_the_vendor_exceptions_can_never_name_a_first_party_file() -> None:
    """Structural, so the list cannot grow into a hole.

    No exception path is in any runtime's allowlist, and none of them is the archive
    entrypoint. Checked rather than assumed, because "narrow" is a claim about a set.
    """

    shipped: set[str] = set()
    for agent in build.RUNTIME_NAMES:
        shipped |= build.load_manifest(agent).archive_first_party

    assert not build.VENDOR_CONTENT_EXCEPTIONS & shipped
    for exception in build.VENDOR_CONTENT_EXCEPTIONS:
        assert not exception.startswith(("src/", "runtimes/")), exception


def test_an_unknown_archive_is_scanned_with_no_exceptions_at_all(tmp_path: Path) -> None:
    """Fail closed: a caller that cannot say what is vendored gets the strict treatment."""

    exempted = next(iter(build.VENDOR_CONTENT_EXCEPTIONS))
    archive_path = tmp_path / "unknown.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr(exempted, FAKE_SECRET_ASSIGNMENT)

    strict = build.inspect_archive(archive_path)
    informed = build.inspect_archive(archive_path, first_party=frozenset({"main.py"}))

    assert any(exempted in problem for problem in strict)
    assert informed == []


# -- P2-B: first-party text is always read, never skipped ---------------------------------------


LATIN1_SOURCE = (
    "# -*- coding: latin-1 -*-\n"
    '"""Une r\u00e9f\u00e9rence \u00e0 la cl\u00e9 priv\u00e9e."""\n'
    "\n"
    "{assignment}\n"
)
"""Valid Python that is **not** valid UTF-8.

A PEP 263 declaration plus a non-ASCII docstring: the interpreter reads it, imports it, and runs
it. A scanner that only tried UTF-8 raised ``UnicodeDecodeError``, returned no finding, and let
the credential through -- which is the whole of Astra P2-B.
"""


def _latin1(assignment: str) -> bytes:
    return LATIN1_SOURCE.format(assignment=assignment).encode("latin-1")


def test_the_latin1_fixture_is_genuinely_undecodable_as_utf8() -> None:
    """A regression whose input decodes cleanly would prove nothing about the repair."""

    payload = _latin1(FAKE_SECRET_ASSIGNMENT)

    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")


def test_a_non_utf8_first_party_module_is_decoded_the_way_python_decodes_it() -> None:
    """The declaration is honoured, so the text scanned is the text that would run."""

    decoded = build.decode_first_party("src/chorus/settings.py", _latin1(FAKE_SECRET_ASSIGNMENT))

    assert "clé privée" in decoded
    assert FAKE_SECRET_ASSIGNMENT in decoded


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        pytest.param("main.py", FAKE_SECRET_ASSIGNMENT, id="secret-in-root-entrypoint"),
        pytest.param("src/chorus/settings.py", FAKE_TOKEN_ASSIGNMENT, id="token-in-chorus"),
        pytest.param("runtimes/server.py", FAKE_ACCESS_KEY_ASSIGNMENT, id="key-in-runtimes"),
    ],
)
def test_a_latin1_first_party_secret_fails_the_staging_scan(
    tmp_path: Path, relative: str, content: str
) -> None:
    """P2-B, at the first gate."""

    staged = tmp_path / "staging"
    path = staged / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_latin1(content))

    problems = build.scan_staged_first_party(staged, [relative])

    assert len(problems) == 1
    assert relative in problems[0]
    assert "credential-shaped content" in problems[0]
    assert content not in problems[0]


def test_a_latin1_first_party_secret_fails_the_final_archive_scan(tmp_path: Path) -> None:
    """P2-B, at the second gate -- injected into the zip after staging."""

    archive_path = tmp_path / "latin1.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("src/chorus/settings.py", _latin1(FAKE_SECRET_ASSIGNMENT))

    problems = build.inspect_archive(
        archive_path, first_party=frozenset({"main.py", "src/chorus/settings.py"})
    )

    assert any("credential-shaped content" in problem for problem in problems)
    assert any("settings.py" in problem for problem in problems)
    assert not any(FAKE_SECRET_ASSIGNMENT in problem for problem in problems)


def test_a_latin1_first_party_secret_injected_after_staging_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation regression: a first-party file cannot evade the gate by changing encoding."""

    real_write = build.write_archive

    def write_and_poison(staging: Path, destination: Path) -> tuple[str, int]:
        digest, count = real_write(staging, destination)
        with zipfile.ZipFile(destination, "a") as archive:
            archive.writestr("main.py", _latin1(FAKE_SECRET_ASSIGNMENT))
        return digest, count

    monkeypatch.setattr(build, "write_archive", write_and_poison)

    with pytest.raises(build.BuildError, match="credential-shaped content"):
        _build("monitor", tmp_path)


def test_first_party_content_that_decodes_under_nothing_fails_closed(tmp_path: Path) -> None:
    """No supported encoding, so no scan is possible -- and no scan means no shipping.

    The bytes are invalid UTF-8 and carry no usable declaration, so the only honest outcomes are
    a finding or a lie. The gate chooses the finding, and the finding names the path only.
    """

    undecodable = b"\xff\xfe\x00garbage that is not text\x00\xff"

    with pytest.raises(build.UndecodableFirstPartyError, match=re.escape("src/chorus/settings.py")):
        build.decode_first_party("src/chorus/settings.py", undecodable)

    staged = tmp_path / "staging"
    (staged / "src" / "chorus").mkdir(parents=True)
    (staged / "src" / "chorus" / "settings.py").write_bytes(undecodable)

    problems = build.scan_staged_first_party(staged, ["src/chorus/settings.py"])

    assert problems == [
        "first-party file is not decodable text and cannot be scanned: src/chorus/settings.py"
    ]


def test_a_first_party_module_declaring_an_unknown_encoding_fails_closed() -> None:
    """A declaration Python itself would reject is not an escape hatch either."""

    source = b"# -*- coding: not-a-real-codec -*-\nVALUE = 1\n"

    with pytest.raises(build.UndecodableFirstPartyError):
        build.decode_first_party("runtimes/server.py", source)


def test_ordinary_utf8_first_party_source_passes(tmp_path: Path) -> None:
    """The control. A clean module is read, scanned, and reported clean."""

    clean = '"""A module with an accented word: r\u00e9sum\u00e9."""\n\nVALUE = 1\n'.encode()

    assert build.decode_first_party("src/chorus/settings.py", clean).endswith("VALUE = 1\n")
    assert build.secret_content_problem("src/chorus/settings.py", clean, first_party=True) is None

    staged = tmp_path / "staging"
    (staged / "src" / "chorus").mkdir(parents=True)
    (staged / "src" / "chorus" / "settings.py").write_bytes(clean)

    assert build.scan_staged_first_party(staged, ["src/chorus/settings.py"]) == []


def test_a_vendored_native_binary_is_not_text_decoded(tmp_path: Path) -> None:
    """The other half of § 6: a compiled extension stays binary.

    Decoding one would report whatever byte sequence resembled a pattern. Its name is still
    checked, its architecture is still checked by ELF header, and its provenance is the lockfile.
    """

    native = bytes(range(256)) * 8

    assert build.secret_content_problem("pydantic_core/_x.so", native, first_party=False) is None

    archive_path = tmp_path / "native.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("pydantic_core/_pydantic_core.so", native)

    assert build.inspect_archive(archive_path, first_party=frozenset({"main.py"})) == []


def test_the_same_bytes_under_a_first_party_path_are_still_refused(tmp_path: Path) -> None:
    """Undecodable is tolerated because a file is *vendored*, never because it is unreadable."""

    native = bytes(range(256)) * 8
    name = "src/chorus/contracts/common.py"

    assert build.secret_content_problem(name, native, first_party=False) is None
    with pytest.raises(build.UndecodableFirstPartyError):
        build.secret_content_problem(name, native, first_party=True)


def test_a_latin1_secret_under_a_vendor_exception_path_is_still_refused_when_first_party() -> None:
    """The two mechanisms compose: no exception, and no skip.

    The exact path the vendor list exempts, carrying the exact content it exempts, encoded the
    way that used to bypass decoding -- rejected outright once the file is first-party.
    """

    # Named rather than drawn from the set: frozenset iteration order follows string hashing,
    # which is randomised per process, and this test depends on the path *not* being ``.py``.
    exempted = "botocore/data/iam/2010-05-08/examples-1.json"
    assert exempted in build.VENDOR_CONTENT_EXCEPTIONS
    payload = _latin1(FAKE_SECRET_ASSIGNMENT)

    # Vendored: the exact exception applies, and the bytes are never read.
    assert build.secret_content_problem(exempted, payload, first_party=False) is None

    # First-party, same path, same bytes: no exception is consulted, and the file is not a
    # ``.py`` so no encoding declaration is honoured -- it cannot be read, so it is refused.
    with pytest.raises(build.UndecodableFirstPartyError, match=re.escape(exempted)):
        build.secret_content_problem(exempted, payload, first_party=True)

    # And as first-party Python, where the declaration *is* honoured, the secret is found.
    found = build.secret_content_problem("src/chorus/settings.py", payload, first_party=True)
    assert found is not None
    assert "credential-shaped content" in found


# -- B: sensitive filenames ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.production",
        ".env.local",
        "credentials",
        "id_rsa",
        "id_ed25519",
        "config/.aws/credentials",
        "home/.ssh/known_hosts",
        "nested/deep/.env.staging",
    ],
)
def test_a_credential_file_is_refused_by_name_whatever_it_contains(
    tmp_path: Path, name: str
) -> None:
    """An empty ``.env.production`` matches no pattern and must still fail the build.

    Its presence is the finding: a build that produced it reached somewhere it should not have.
    """

    assert build.sensitive_name_problem(name) is not None

    archive_path = tmp_path / "named.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr(name, "")

    problems = build.inspect_archive(archive_path, first_party=frozenset({"main.py"}))

    assert any(name in problem for problem in problems)


def test_private_key_material_is_detected_by_content_not_by_extension(tmp_path: Path) -> None:
    """``certifi/cacert.pem`` is public CA certificates and every TLS call depends on it.

    Banning ``.pem`` would refuse a correct archive, so the rule is about what a private key
    says in its first line -- which a certificate bundle never says.
    """

    archive_path = tmp_path / "keys.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr(
            "certifi/cacert.pem",
            "-----BEGIN CERTIFICATE-----\nnot-a-real-certificate\n-----END CERTIFICATE-----\n",
        )
        archive.writestr("vendor/leaked.pem", FAKE_PRIVATE_KEY)

    problems = build.inspect_archive(archive_path, first_party=frozenset({"main.py"}))

    assert not any("cacert.pem" in problem for problem in problems)
    assert any(
        "private-key material" in problem and "leaked.pem" in problem for problem in problems
    )


def test_a_binary_member_is_not_decoded_and_reported(tmp_path: Path) -> None:
    """A compiled extension is not text; decoding one reports byte noise, not a secret."""

    archive_path = tmp_path / "binary.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("pydantic_core/_pydantic_core.so", bytes(range(256)) * 8)

    assert build.inspect_archive(archive_path, first_party=frozenset({"main.py"})) == []


# -- C: the final zip, and the build that refuses to publish one -------------------------------


def _fake_install(target: Path) -> None:
    package = target / "pydantic"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("VERSION = '2'\n", encoding="utf-8")


def _build(agent: str, output: Path, *, after_staging: object = None) -> build.BuiltArtifact:
    def runner(command: list[str], cwd: Path) -> None:
        if command[:3] == ["uv", "pip", "install"]:
            _fake_install(Path(command[command.index("--target") + 1]))

    return build.build_runtime(agent, output_root=output, runner=runner)


def test_a_secret_written_into_a_first_party_file_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the staging gate stops the build before an archive is written."""

    real_stage = build.stage_sources

    def stage_and_poison(
        manifest: build.RuntimeManifest, *, repository: Path, staging: Path
    ) -> None:
        real_stage(manifest, repository=repository, staging=staging)
        (staging / "main.py").write_text(f"KEY = '{FAKE_ACCESS_KEY}'\n", encoding="utf-8")

    monkeypatch.setattr(build, "stage_sources", stage_and_poison)

    with pytest.raises(build.BuildError, match="credential-shaped content"):
        _build("monitor", tmp_path)

    assert not (tmp_path / "runtime-monitor.zip").exists(), "nothing is written past a refusal"


def test_a_secret_introduced_after_staging_still_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the final zip is scanned as well as the staging tree.

    Here the archive is written with an extra member the staging scan never saw -- which is what
    a builder bug, or anything that touched the archive between the two steps, would look like.
    """

    real_write = build.write_archive

    def write_and_poison(staging: Path, destination: Path) -> tuple[str, int]:
        digest, count = real_write(staging, destination)
        with zipfile.ZipFile(destination, "a") as archive:
            archive.writestr("smuggled.py", f"TOKEN = '{FAKE_ACCESS_KEY}'\n")
        return digest, count + 1

    monkeypatch.setattr(build, "write_archive", write_and_poison)

    with pytest.raises(build.BuildError, match="credential-shaped content"):
        _build("monitor", tmp_path)


def test_a_credential_file_added_after_staging_still_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = build.write_archive

    def write_and_poison(staging: Path, destination: Path) -> tuple[str, int]:
        digest, count = real_write(staging, destination)
        with zipfile.ZipFile(destination, "a") as archive:
            archive.writestr(".env.production", "")
        return digest, count + 1

    monkeypatch.setattr(build, "write_archive", write_and_poison)

    with pytest.raises(build.BuildError, match="environment file"):
        _build("monitor", tmp_path)


# -- D: what the manifest records ---------------------------------------------------------------


def test_the_manifest_records_that_a_scan_ran_and_carries_no_secret(tmp_path: Path) -> None:
    artifacts = [_build(agent, tmp_path) for agent in build.RUNTIME_NAMES]

    path = build.write_manifest_file(artifacts, output_root=tmp_path)
    document = path.read_text(encoding="utf-8")

    import json

    parsed = json.loads(document)
    for record in parsed["artifacts"]:
        assert record["security_scan"] == build.ARTIFACT_SCANNER_VERSION
    for pattern in PATTERNS:
        assert not pattern.search(document)


def test_the_built_artifacts_pass_the_gate_as_they_stand() -> None:
    """The clean case, over the real archives, so a green suite means something was scanned."""

    for agent in build.RUNTIME_NAMES:
        archive_path = build.DEFAULT_OUTPUT_ROOT / f"runtime-{agent}.zip"
        if not archive_path.is_file():
            pytest.skip(
                "artifacts are not built; run `uv run python -m tools.build_runtime_artifacts`"
            )
        manifest = build.load_manifest(agent)
        problems = build.inspect_archive(
            archive_path,
            target_platform=manifest.target_platform,
            first_party=manifest.archive_first_party,
        )
        assert problems == [], problems
