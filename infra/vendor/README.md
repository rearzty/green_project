# Установщик ODA File Converter для сборки образа

`ODAFileConverter_QT6_lnxX64_11dll_27.9.deb` — ODA File Converter 27.9 для
Linux x86-64, с официальной страницы opendesign.com (SHA-256
`12f5e14c55c35176062f63e242d5390f732722213dd685fa3cd44a6555384cd0`).

Лежит в репозитории потому, что сайт ODA закрыт для России: из РФ загрузка
отдаёт 403 «Access restricted» (страна под санкциями OFAC), и без этого файла
backend собирался бы с запасным LibreDWG — а тот теряет заливки покрытий
(`REGION`: проезжая часть, тротуары, газоны) и не читает часть чертежей (см.
`geo_engine/io/dwg_convert.py`). Использовать ODA в решении организаторы
разрешили.

`infra/Dockerfile.backend` берёт установщик отсюда и только если его нет —
пробует сеть. В логе сборки: `ODA File Converter: взят из infra/vendor`.
ODA существует только под x86-64; на arm64 образ собирается с LibreDWG.
