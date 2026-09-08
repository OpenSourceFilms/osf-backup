"""Regression tests locking in path-before-extension classification.

These exist because of a real finding: BTS/fight_test_19.6.26/Noah_BTS/
A001_02051406_C026.mov is a RED-camera-named .mov sitting inside the BTS
tree, while footage/ holds ~994 GB of RED .r3d as genuine principal
photography. Extension alone cannot tell these apart -- only path can.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import policy as P  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"


def _policy():
    return P.Policy.from_yaml(CONFIG)


def test_red_named_file_inside_bts_is_included():
    d = _policy().classify(
        "BTS/fight_test_19.6.26/Noah_BTS/A001_02051406_C026.mov", file_size=1000
    )
    assert d.classification == P.Classification.BTS
    assert d.selected is True


def test_red_file_inside_footage_is_excluded_regardless_of_extension():
    d = _policy().classify("footage/DAY_1/RUSHES/A CAM/A001_test.r3d", file_size=1000)
    assert d.classification == P.Classification.PRINCIPAL
    assert d.selected is False


def test_same_extension_different_path_different_class():
    """Same .mov extension, opposite classification, purely by path."""
    pol = _policy()
    bts = pol.classify("BTS/Daniel_BTS/IMG_5133.MOV", file_size=1000)
    principal = pol.classify("footage/DAY_2/RUSHES/B CAM/B015_C020_0924DU_002.mov", file_size=1000)
    assert bts.classification == P.Classification.BTS and bts.selected is True
    assert principal.classification == P.Classification.PRINCIPAL and principal.selected is False


def test_principal_and_redownloadable_are_never_selected():
    pol = _policy()
    assert pol.classify("footage/DJI/x.MP4", file_size=1).selected is False
    assert pol.classify(".cache/huggingface/model.bin", file_size=1).selected is False
    assert pol.classify("comfy/models_shared/checkpoints/x.safetensors", file_size=1).selected is False


def test_critical_and_bts_bypass_output_quota():
    pol = _policy()
    huge = 999_999_999_999
    assert pol.classify("erf/README.md", file_size=huge).selected is True
    assert pol.classify("BTS/huge.mov", file_size=huge).selected is True


def test_output_file_over_per_file_cap_is_rejected():
    d = _policy().classify("erf/media/huge_output.mp4", file_size=5 * 1024**3)
    assert d.selected is False
    assert "per-file max exceeded" in d.responsible_rule


def test_unclassified_is_reported_not_dropped_or_included():
    d = _policy().classify("some/totally/unknown/newtree/file.bin", file_size=1)
    assert d.classification == P.Classification.UNCLASSIFIED
    assert d.selected is False


# ---------------------------------------------------------------------------
# Wildcard path-rule matcher (2026-08-19)
#
# Found this session: config/policy.yaml has always declared
# "POC_VFX/Shots/*/plate" under paths.principal (comment: "camera-original
# plates copied into VFX shot trees"), but _matches_prefix did literal
# per-component tuple equality, so the '*' never matched anything and the
# rule was dead from the day it was written -- every file under every
# shot's plate/ directory silently fell through to OUTPUTS instead of being
# excluded as principal-duplicate. Same root cause as the earlier dead
# "**/__pycache__" / "**/.venv" globs (already fixed at the scanner level,
# not here). These tests lock in the fix and, per Daniel's instruction, are
# meant to make it impossible for a wildcard path rule to silently become
# dead again -- both by exercising the matcher directly and by asserting
# real policy.yaml rules actually select the files they're meant to.
# ---------------------------------------------------------------------------


def test_bare_star_component_matches_any_single_segment():
    prefix = ("POC_VFX", "Shots", "*", "plate")
    assert P._matches_prefix(("POC_VFX", "Shots", "mask_throw", "plate", "x.mov"), prefix)
    assert P._matches_prefix(("POC_VFX", "Shots", "gun_catch", "plate", "y.mov"), prefix)


def test_star_component_does_not_match_zero_or_multiple_segments():
    prefix = ("POC_VFX", "Shots", "*", "plate")
    # zero segments where '*' sits
    assert not P._matches_prefix(("POC_VFX", "Shots", "plate", "x.mov"), prefix)
    # sibling directory, not "plate"
    assert not P._matches_prefix(("POC_VFX", "Shots", "mask_throw", "COMP", "x.mov"), prefix)


def test_literal_components_still_require_exact_match():
    prefix = ("BTS", "Daniel_BTS")
    assert P._matches_prefix(("BTS", "Daniel_BTS", "IMG_1.MOV"), prefix)
    assert not P._matches_prefix(("BTS", "Other_BTS", "IMG_1.MOV"), prefix)


def test_recursive_glob_component_raises_at_load_time_instead_of_silently_matching_nothing():
    import pytest

    with pytest.raises(ValueError, match="recursive glob"):
        P._validate_rule_parts(("**", "__pycache__"), "paths:redownloadable:**/__pycache__")


def test_partial_segment_glob_raises_at_load_time():
    import pytest

    with pytest.raises(ValueError, match="Unsupported wildcard"):
        P._validate_rule_parts(("Shots", "foo*"), "paths:outputs:Shots/foo*")
    with pytest.raises(ValueError, match="Unsupported wildcard"):
        P._validate_rule_parts(("Shots", "*bar"), "paths:outputs:Shots/*bar")


def test_bare_star_and_literal_parts_pass_validation():
    # must not raise
    P._validate_rule_parts(("POC_VFX", "Shots", "*", "plate"), "paths:principal:test")
    P._validate_rule_parts(("BTS", "Daniel_BTS"), "paths:bts:test")


def test_real_policy_plate_rule_excludes_red_matched_transcodes_not_original_vfx_work():
    """Integration check against the live config/policy.yaml, not a synthetic
    fixture: the exact cases audited this session. If this rule ever goes
    dead again (bad edit, matcher regression, YAML reordering), this fails."""
    pol = _policy()

    # RED-camera-matched transcodes (confirmed same shot code exists as
    # raw .R3D under footage/DAY_N/RUSHES) must be excluded as PRINCIPAL.
    red_matched = [
        "POC_VFX/Shots/mask_throw/plate/B015_C022_0924UO.mov",
        "POC_VFX/Shots/gun_catch/plate/B015_C031_0924QO.mov",
        "POC_VFX/Shots/turn_to_door/Plate/A003_B015_09245Y.mov",
        "POC_VFX/Shots/drop_masks/Plate/A021_B010_0923TE.mov",
        "POC_VFX/Shots/drop_masks/Workplates/A021_B010_0923TE_3k_ProRes422LT.mov",
        "POC_VFX/Shots/light_sconces/Plate/B003_C015_0925IG.mov",
        "POC_VFX/Shots/drop_into_frame/C003_C002_0924EJ.mov",
    ]
    for path in red_matched:
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.PRINCIPAL, path
        assert d.selected is False, path

    # Original VFX work sitting inside a plate/Plate-named directory (no RED
    # match found) must stay protected as OUTPUTS, not get swept up by the
    # broader plate/ exclusion.
    original_work = [
        "POC_VFX/Shots/orbit_and_turn/plate/orbit_and_turn_01.mov",
        "POC_VFX/Shots/light_sconces/Plate/Chapel_fire&sconces.mov",
        "POC_VFX/Shots/light_sconces/Plate/plate with fire.mov",
        "POC_VFX/Shots/sword_test/Plate/swordtest_01.mov",
    ]
    for path in original_work:
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.OUTPUTS, path
        assert d.selected is True, path

    # Comp/output deliverables (never matched a plate rule at all) unaffected.
    d = pol.classify("POC_VFX/Shots/mask_throw/COMP/final.mov", file_size=1000)
    assert d.classification == P.Classification.OUTPUTS
    assert d.selected is True


# ---------------------------------------------------------------------------
# Daniel's 2026-08-19 rulings: top-level catalog/ and /workspace/.claude/
# ---------------------------------------------------------------------------

def test_top_level_catalog_is_protected():
    """~0.3 GiB, trivial at the chosen backup capacity; reconstructability
    from pipeline/'s raw inputs is unconfirmed, so keep it rather than risk
    data loss to save 300 MB."""
    pol = _policy()
    d = pol.classify("catalog/asr/some_media_id.json", file_size=1000)
    assert d.classification == P.Classification.CRITICAL
    assert d.selected is True


def test_catalog_derived_subdirs_stay_redownloadable():
    """Longest-prefix-wins: the pre-existing derived-only subdirs (logs,
    status, milvus vector index) must still be excluded even though the
    parent catalog/ is now CRITICAL."""
    pol = _policy()
    for path in (
        "catalog/logs/run.log",
        "catalog/status/heartbeat.json",
        "catalog/milvus/collection.dat",
    ):
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.REDOWNLOADABLE, path
        assert d.selected is False, path


def test_claude_session_and_file_history_are_excluded():
    pol = _policy()
    for path in (
        ".claude/projects/-workspace--BACKUP/some-session.jsonl",
        ".claude/file-history/2026-08-19/some-edit.diff",
    ):
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.REDOWNLOADABLE, path
        assert d.selected is False, path


def test_claude_authored_config_is_protected():
    """CLAUDE.md and settings/config files -- the explicitly authored
    operational material Daniel's ruling named -- must stay CRITICAL."""
    pol = _policy()
    for path in (
        ".claude/CLAUDE.md",
        ".claude/settings.json",
        ".claude/settings.local.json",
    ):
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.CRITICAL, path
        assert d.selected is True, path


def test_claude_credentials_file_is_hard_excluded():
    """Not part of Daniel's explicit ruling -- added by inference from the
    existing credentials.env precedent. Locking in current behavior so a
    future change here is a deliberate edit, not a silent regression."""
    pol = _policy()
    d = pol.classify(".claude/.credentials.json", file_size=1000)
    assert d.classification == P.Classification.EXCLUDED_HARD
    assert d.selected is False


# ---------------------------------------------------------------------------
# 2026-08-23: erf/media derived-subtree ruling encoded into policy.yaml.
#
# keyframes/proxies/thumbs are pipeline-regenerable derived media; the
# adjudication existed only in conversation and was never in policy.yaml,
# so the broad `outputs: - erf/media` rule over-protected them. These tests
# exercise the real policy.yaml/matcher, not a synthetic fixture, so a bad
# edit or matcher regression here fails loudly instead of silently
# re-selecting ~4.68 GiB of regenerable media.
# ---------------------------------------------------------------------------


def test_erf_media_derived_subtrees_are_redownloadable():
    pol = _policy()
    for path in (
        "erf/media/keyframes/m_3616a3cc75b9/m_3616a3cc75b9_kf_15833.jpg",
        "erf/media/proxies/m_f00a8c3cc170.mp4",
        "erf/media/thumbs/m_037e63ba6a9a/m_037e63ba6a9a_mid.webp",
    ):
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.REDOWNLOADABLE, path
        assert d.selected is False, path


def test_erf_media_music_risk_review_stays_protected():
    """Not a derived subtree -- must stay OUTPUTS/selected even though it
    sits alongside the newly-excluded keyframes/proxies/thumbs subtrees."""
    pol = _policy()
    d = pol.classify(
        "erf/media/music_risk_review/10_20250917_173620.mp4", file_size=1000
    )
    assert d.classification == P.Classification.OUTPUTS
    assert d.selected is True


# ---------------------------------------------------------------------------
# 2026-08-23: policy-conformance audit -- additional findings beyond
# erf/media, same session. Each of these exercises the real policy.yaml/
# matcher, not a synthetic fixture.
# ---------------------------------------------------------------------------


def test_platelock_env_venv_is_redownloadable_but_rest_of_platelock_is_not():
    """platelock/env is a conventional `python3.12 -m venv` (pyvenv.cfg
    confirmed) sitting under the broad `outputs: - platelock` rule with no
    override -- 8.6 GiB backed up unnecessarily. The rest of platelock/
    (authored code/data) must stay OUTPUTS/selected."""
    pol = _policy()
    d = pol.classify(
        "platelock/env/platewarp/lib/python3.12/site-packages/x.so", file_size=1000
    )
    assert d.classification == P.Classification.REDOWNLOADABLE
    assert d.selected is False

    d2 = pol.classify("platelock/data/some_authored_data.json", file_size=1000)
    assert d2.classification == P.Classification.OUTPUTS
    assert d2.selected is True


def test_houdini_caches_redownloadable_rule_is_no_longer_dead():
    """Was previously listed under BOTH critical (identical prefix, parsed
    first, silently won every tie) and redownloadable -- the redownloadable
    rule was dead from the day it was written. Audited 2026-08-23: the
    entire tree is 2 Flame*.zip VDB cache archives, nothing authored, so the
    critical entry was removed rather than reordered blindly."""
    pol = _policy()
    d = pol.classify(
        "POC_VFX/GeneralAssets/houdini_caches/sconce_flames_ignite/Flame_vdb.zip",
        file_size=1000,
    )
    assert d.classification == P.Classification.REDOWNLOADABLE
    assert d.selected is False


def test_poc_vfx_general_assets_sibling_of_houdini_caches_stays_protected():
    """Proves the houdini_caches correction did not broaden the exclusion --
    an adjacent authored file directly under POC_VFX/GeneralAssets (not
    inside houdini_caches or RoomScan/OLD) must stay OUTPUTS/selected."""
    pol = _policy()
    d = pol.classify(
        "POC_VFX/GeneralAssets/some_other_authored_asset.hip", file_size=1000
    )
    assert d.classification == P.Classification.OUTPUTS
    assert d.selected is True


def test_table_read_braw_decode_is_now_protected():
    """Prior 'keep the whole directory' decision existed only in BATON.md,
    never encoded in policy.yaml -- was UNCLASSIFIED. Mixed authored
    source/QA-stills/logs, 124 MiB, kept whole per the original call."""
    pol = _policy()
    d = pol.classify("table_read/braw_decode/braw_decode.cpp", file_size=1000)
    assert d.classification == P.Classification.CRITICAL
    assert d.selected is True


def test_table_read_media_partial_c001_is_protected():
    """The only local copy, in any form, of the C001 camera roll -- an
    incomplete/recovery-pending 19.26 GiB BRAW file. Was UNCLASSIFIED (not
    backed up at all) before this session's audit."""
    pol = _policy()
    d = pol.classify(
        "table_read/Media/3018_10171415_C001.braw.PARTIAL",
        file_size=19_260_243_968,
    )
    assert d.classification == P.Classification.CRITICAL
    assert d.selected is True


def test_table_read_media_h264_is_protected():
    """No raw .braw counterpart found on /workspace for C002-C005 -- these
    H.264 files are the only local copies of that footage. Was
    UNCLASSIFIED."""
    pol = _policy()
    d = pol.classify(
        "table_read/Media_H264/3018_10171700_C004.mp4", file_size=1000
    )
    assert d.classification == P.Classification.CRITICAL
    assert d.selected is True


def test_website_redundant_wordpress_dumps_are_redownloadable():
    """Three near-duplicate WordPress backups (dump, WeTransfer export,
    recovery copy) under website/ -- ~44,000 tiny files dominating the
    2026-08-23 seed's upload tail on per-file latency. Daniel's call,
    2026-08-23: exclude as redundant."""
    pol = _policy()
    for path in (
        "website/DL_Dump/wp-content/plugins/elementor/assets/js/x.js",
        "website/wetransfer_u797478838_xubk0-3-sql_2025-11-25_2206/wp-includes/blocks/x.php",
        "website/recovery/wp-includes/blocks/x.php",
        "website/recovery-backups/x.tar",
    ):
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.REDOWNLOADABLE, path
        assert d.selected is False, path


def test_website_dl_dump_zip_is_redownloadable():
    """website/DL_Dump.zip is a ~455 MiB zipped copy of the same DL_Dump/
    WordPress dump tree ruled REDOWNLOADABLE above -- a sibling *file*, not
    caught by that directory's prefix rule (component-boundary matching
    means "DL_Dump.zip" != "DL_Dump"). Confirmed via `unzip -l` 2026-08-27:
    19,591 entries, identical wp-admin/wp-content/wp-includes layout. Was
    still OUTPUTS/selected=True as of backup-2026-08-23; needs its own rule."""
    pol = _policy()
    d = pol.classify("website/DL_Dump.zip", file_size=455_500_000)
    assert d.classification == P.Classification.REDOWNLOADABLE
    assert d.selected is False


def test_website_other_content_stays_protected():
    """Proves the redundant-dump exclusion didn't broaden to the rest of
    website/ -- loose files and other subtrees must stay OUTPUTS/selected."""
    pol = _policy()
    for path in ("website/index.html", "website/Docs/notes.txt", "website/u797478838_xubK0.sql"):
        d = pol.classify(path, file_size=1000)
        assert d.classification == P.Classification.OUTPUTS, path
        assert d.selected is True, path


# --------------------------------------------------------------------------
# 2026-09-07 unprotected-tree adjudication.
#
# Seven trees were found UNCLASSIFIED/selected=False with no rule at any
# depth and no git repo to fall back on -- i.e. never backed up at all. The
# sharpest was melb_acquire: its subdirectories were ruled long ago, but
# nothing covered files at the tree root, which is where its BATON.md,
# DECISIONS.md and audit reports live. These lock in each ruling AND, for
# every tree that mixes authored material with rebuildable bulk, prove the
# demotion still carves the bulk back out (longest-prefix-wins).
# --------------------------------------------------------------------------

NEWLY_ADJUDICATED = [
    # (path, classification, selected)
    ("melb_acquire/BATON.md", P.Classification.CRITICAL, True),
    ("melb_acquire/DECISIONS.md", P.Classification.CRITICAL, True),
    ("melb_acquire/OPUS_PREEXPORT_AUDIT_2026-09-07.md", P.Classification.CRITICAL, True),
    ("melb_acquire/ASIC_bulk/asic.zip", P.Classification.CRITICAL, True),
    ("melb_acquire/.venv/lib/python3.13/site-packages/x.py", P.Classification.REDOWNLOADABLE, False),
    ("melb_acquire/.rtfm/index.db", P.Classification.REDOWNLOADABLE, False),
    ("sparkvsr-pulse-lab/G4_G5_TECHNICAL_RECORD.md", P.Classification.CRITICAL, True),
    ("sparkvsr-pulse-lab/scripts/run_phase3.sh", P.Classification.CRITICAL, True),
    ("sparkvsr-pulse-lab/runs/g4/out.mp4", P.Classification.OUTPUTS, True),
    ("sparkvsr-prod/MODIFICATIONS.md", P.Classification.CRITICAL, True),
    ("sparkvsr-prod/supir_v0f_template_workflow.json", P.Classification.CRITICAL, True),
    ("sparkvsr-prod/models/supir.safetensors", P.Classification.REDOWNLOADABLE, False),
    ("sparkvsr-prod/output/frame.png", P.Classification.OUTPUTS, True),
    ("three_ghosts/S03_FROZEN.md", P.Classification.CRITICAL, True),
    ("three_ghosts/calibration/cam.json", P.Classification.CRITICAL, True),
    ("three_ghosts/media/A.mp4", P.Classification.OUTPUTS, True),
    ("_incident_2026-09-04/SHA256SUMS.txt", P.Classification.CRITICAL, True),
    ("_incident_2026-09-04/copyparty.log.snapshot", P.Classification.CRITICAL, True),
    ("logs/copyparty.log", P.Classification.OUTPUTS, True),
    ("github/BATON.md", P.Classification.CRITICAL, True),
    ("github/licensing.txt", P.Classification.CRITICAL, True),
    ("github/.claude/settings.json", P.Classification.CRITICAL, True),
    ("github/runpod_renewal/notes.md", P.Classification.CRITICAL, True),
    ("github/.rtfm/index.db", P.Classification.REDOWNLOADABLE, False),
]


@pytest.mark.parametrize("path,expected_class,expected_selected", NEWLY_ADJUDICATED)
def test_2026_09_07_adjudications(path, expected_class, expected_selected):
    d = _policy().classify(path, file_size=1000)
    assert d.classification == expected_class, f"{path} -> {d.classification}"
    assert d.selected is expected_selected, f"{path} -> selected={d.selected}"


def test_github_repo_mirrors_are_left_to_git_classification():
    """A blanket `github` path rule would have been wrong: path rules
    OUTRANK git classification, so it would force full-repo backups of 14
    mirrors that already have GitHub remotes. Only the loose material is
    ruled by path; anything inside a mirror must stay unruled here so the
    git-aware pass decides it."""
    d = _policy().classify("github/opensourcefilms-erf/README.md", file_size=1000)
    assert d.classification == P.Classification.UNCLASSIFIED
    assert d.selected is False


def test_melb_acquire_root_rule_did_not_disturb_existing_subdir_rulings():
    """The new tree-root rule must not have swallowed the rulings that were
    already in force under it -- longest-prefix-wins is the whole basis for
    adding a root rule to an already-ruled tree."""
    pol = _policy()
    for path, expected in (
        ("melb_acquire/ABN_bulk/bulk.zip", P.Classification.CRITICAL),
        ("melb_acquire/output/result.csv", P.Classification.CRITICAL),
        ("melb_acquire/data/raw.csv", P.Classification.OUTPUTS),
        ("melb_acquire/_archive/old.csv", P.Classification.OUTPUTS),
        ("melb_acquire/logs/run.log", P.Classification.REDOWNLOADABLE),
    ):
        assert pol.classify(path, file_size=1000).classification == expected, path
