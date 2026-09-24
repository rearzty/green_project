"""ezdxf's MTEXT column-layout loader has no fallback for an out-of-range
column type -- live on 13. Харьковский проезд, a DWG's separately-supplied
electrical drawing (`ЭС_Харьковский проезд.dwg`) carries embedded XDATA
(`ACAD_MTEXT_COLUMN_INFO`) that, after conversion through dwg2dxf, decodes to
a value no `ColumnType` member matches (1434 seen live). `enum.IntEnum` has
no fallback of its own, and this is Python enum construction during entity
loading, not DXF group-code tokenizing, so neither `ezdxf.readfile()` nor its
own `ezdxf.recover` fallback survives it -- both raise the same
`ValueError: 1434 is not a valid ColumnType`.

`geo_engine.io.dxf_reader` patches `ColumnType._missing_` at import time (see
its module-level comment) so an unrecognised value resolves to `NONE`
(MTEXT's default, no special column layout) instead of raising -- this
project never reads MTEXT column layout, only geometry, so the fallback
value itself is inert.
"""

import ezdxf.entities.mtext

import geo_engine.io.dxf_reader  # noqa: F401 -- import applies the patch


def test_an_unrecognised_column_type_value_does_not_raise():
    assert ezdxf.entities.mtext.ColumnType(1434) is ezdxf.entities.mtext.ColumnType.NONE


def test_well_formed_column_type_values_still_resolve_correctly():
    assert ezdxf.entities.mtext.ColumnType(1) is ezdxf.entities.mtext.ColumnType.STATIC
    assert ezdxf.entities.mtext.ColumnType(2) is ezdxf.entities.mtext.ColumnType.DYNAMIC
