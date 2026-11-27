from goopdl.profiles import patch_profile_country


def test_patch_profile_country_sets_full_mcc_mnc():
    out = patch_profile_country({"CellOperator": "310260", "SimOperator": "310260"}, "de")
    assert out["CellOperator"] == out["SimOperator"] == "26201"
