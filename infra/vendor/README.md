# Локальные установщики для сборки образа

Сайт ODA (opendesign.com) закрыт для России: из РФ загрузка отдаёт 403
«Access restricted» (страна под санкциями OFAC). Без ODA File Converter
backend собирается с запасным LibreDWG: теряются заливки покрытий, часть
чертежей не читается (см. `geo_engine/io/dwg_convert.py`).

Положите сюда установщик `ODAFileConverter_QT6_lnxX64_*.deb` (скачанный там,
где сайт доступен) — `infra/Dockerfile.backend` возьмёт его вместо загрузки
из сети. В лог сборки попадёт строка `ODA File Converter: взят из infra/vendor`.

Сами `.deb` в git не кладутся (`.gitignore`): лицензия ODA не разрешает
распространять установщик.
