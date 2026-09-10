from amdl.pipeline import file_exists


def test_interrupted_empty_output_is_not_complete(tmp_path):
    output = tmp_path / "interrupted.m4a"
    output.touch()
    assert not file_exists(str(output))
    output.write_bytes(b"completed output")
    assert file_exists(str(output))
