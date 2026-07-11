"""Troceo de mensajes largos para el límite de 4096 caracteres de Telegram."""
from app.telegram import split_message


def test_mensaje_corto_no_se_trocea():
    assert split_message("hola") == ["hola"]


def test_trocea_por_lineas_sin_perder_texto():
    text = "\n".join(f"🟢 <b>TICKER{i}</b>  123,45 $  (+1,23%)" for i in range(100))
    parts = split_message(text, limit=200)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)
    # Ningún trozo corta una línea por la mitad: al reunirlos sale el original.
    assert "\n".join(parts) == text


def test_linea_mas_larga_que_el_limite_se_corta():
    parts = split_message("x" * 950, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts) == "x" * 950


def test_linea_gigante_tras_texto_normal_mantiene_el_orden():
    text = "primera línea\n" + "y" * 500
    parts = split_message(text, limit=300)
    assert parts[0] == "primera línea"
    assert "".join(parts[1:]) == "y" * 500
