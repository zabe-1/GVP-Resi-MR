from cre_market.cli import load_batch


def test_load_batch_names_coords_and_costar(tmp_path):
    costar = tmp_path / "rent.csv"
    costar.write_text("x\n")
    p = tmp_path / "batch.csv"
    p.write_text(
        "name,address,latitude,longitude,notes,costar_rent\n"
        'Asset A,"1 Main St, Houston, TX",29.76,-95.36,approx pin,rent.csv\n'
        'Asset B,"2 Oak St, Dallas, TX",,,,\n'
        ',"3 Elm St, Austin, TX",bad,,ignored,\n')
    jobs = load_batch(str(p))
    assert len(jobs) == 3
    a, b, c = jobs
    assert a["name"] == "Asset A" and a["lat"] == 29.76 and a["lon"] == -95.36
    assert a["costar"]["rent"].endswith("rent.csv")
    assert b["lat"] is None and b["lon"] is None and b["costar"] == {}
    assert c["name"] == "" and c["lat"] is None   # unparseable coord -> geocode


def test_load_batch_address_only_header(tmp_path):
    p = tmp_path / "batch.csv"
    p.write_text('address\n"1 Main St, Houston, TX"\n\n')
    jobs = load_batch(str(p))
    assert len(jobs) == 1
    assert jobs[0]["address"] == "1 Main St, Houston, TX"
