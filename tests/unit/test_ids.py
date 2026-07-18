from etf_quant_lab.ids import FixedIdGenerator, UlidGenerator


def test_ulid_generator_returns_opaque_sortable_shape() -> None:
    generated = UlidGenerator().new()

    assert len(generated) == 26
    assert generated.isalnum()
    assert generated == generated.upper()


def test_fixed_id_generator_is_deterministic() -> None:
    generator = FixedIdGenerator(["01K0D7F7P6XQ4M2Z8H9B3C5N12"])

    assert generator.new() == "01K0D7F7P6XQ4M2Z8H9B3C5N12"
