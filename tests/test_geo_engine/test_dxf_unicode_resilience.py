"""ezdxf's own \\U+xxxx-escape decoder has no fallback for a truncated
escape -- live on 3 pilot streets (Старый Гай, Берзарина, Академика
Понтрягина), a DWG's embedded material/plot-style metadata carried a raw
byte invalid in the file's stated encoding; ezdxf decodes that with
errors="surrogateescape", leaving a lone UTF-16 surrogate glued to whatever
text follows -- and a `\\U+` sequence landing right next to it, with fewer
than 4 hex digits after it, makes `ezdxf.lldxf.encoding._decode()` raise
ValueError with no fallback, in *both* the strict reader and its own
recover-mode fallback. `geo_engine.io.dxf_reader` patches `_decode` at
import time (see its module-level comment) to leave an escape it cannot
parse as literal text instead of aborting the whole file.

Importing `geo_engine.io.dxf_reader` is exactly what applies the patch --
these tests exercise `ezdxf.lldxf.encoding` directly, not through a
constructed DXF file, because `ezdxf.new().saveas()` only ever writes
well-formed escapes; the corruption here is a property of the *string*
handed to the decoder, not of DXF structure a synthetic fixture could
reproduce.
"""

import pytest

import ezdxf.lldxf.encoding as dxf_encoding

import geo_engine.io.dxf_reader  # noqa: F401 -- import applies the patch


def test_truncated_unicode_escape_does_not_raise():
    # A well-formed escape immediately followed by a truncated one (no hex
    # digits at all) -- the exact shape of the live corruption.
    result = dxf_encoding.decode_dxf_unicode("\\U+041F\\U+")
    assert isinstance(result, str)


def test_well_formed_escapes_still_decode_correctly():
    # The patch must not degrade the happy path it wraps.
    assert dxf_encoding.decode_dxf_unicode("\\U+041F\\U+041D\\U+0414") == "ПНД"


def test_garbage_after_backslash_u_plus_does_not_raise():
    # Not just an empty tail -- non-hex text can follow just as easily.
    result = dxf_encoding.decode_dxf_unicode("\\U+garbage")
    assert isinstance(result, str)


class TestMissingLayoutDoesNotAbortTheDocument:
    """`Layouts.get_layout_by_key()` нарушала собственный контракт.

    Живой отказ на «12. Наташинский пр-д» (`04.Графическая часть.dxf`):
    загрузка падала `KeyError: 'ГЕНПЛАН2.1'`, файл целиком пропускался, а
    вместе с ним терялась граница участка — улица переставала считаться
    из-за одной записи в таблице лейаутов.

    Функция на каждом пути отказа поднимает `DXFKeyError`, и вызывающий
    `DXFGraphic.get_layout()` ловит ровно его. Но последняя строка
    обращается к словарю `_layouts` напрямую и бросает ГОЛЫЙ `KeyError`,
    мимо контракта — он проходит сквозь обработчик и убивает загрузку,
    причём в пост-обработке MTEXT-колонок, которую `ezdxf.recover` уже не
    страхует.

    Проверяется ровно это превращение, а не документ целиком: рассинхрон
    таблицы лейаутов через `ezdxf.new().saveas()` не воспроизвести — тот
    всегда пишет согласованный файл, как и с `\\U+`-escape выше.
    """

    def test_a_bare_keyerror_becomes_the_documented_dxf_error(self, monkeypatch):
        import ezdxf
        from ezdxf.layouts.layouts import Layouts
        from ezdxf.lldxf.const import DXFKeyError

        import geo_engine.io.dxf_reader  # noqa: F401 — импорт и применяет патч

        doc = ezdxf.new()
        # Ключ берётся ДО подмены: `doc.modelspace()` сам зовёт `get("Model")`,
        # и вычисление аргумента иначе падало бы раньше проверяемого вызова.
        key = doc.modelspace().block_record.dxf.handle
        # Ровно тот промах, что на реальных данных: имя лейаута в записи
        # блока есть, в словаре его нет.
        monkeypatch.setattr(Layouts, "get", lambda self, name: (_ for _ in ()).throw(KeyError(name)))

        with pytest.raises(DXFKeyError):
            doc.layouts.get_layout_by_key(key)

    def test_the_caller_no_longer_crashes(self, monkeypatch):
        """Ради чего патч и делался: `get_layout()` обязан пережить промах,
        а не уронить документ. Что именно он вернёт — лейаут блока или
        `None` — его дело; важно, что исключение наружу не выходит.
        """
        import ezdxf
        from ezdxf.layouts.layouts import Layouts

        import geo_engine.io.dxf_reader  # noqa: F401

        doc = ezdxf.new()
        text = doc.modelspace().add_mtext("проверка")  # до подмены, см. соседний тест
        monkeypatch.setattr(Layouts, "get", lambda self, name: (_ for _ in ()).throw(KeyError(name)))

        text.get_layout()  # не должно бросать
