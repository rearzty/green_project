"""Пул процессов, который не наследует чужих блокировок.

На Linux `ProcessPoolExecutor` по умолчанию порождает процессы через `fork`:
ребёнок получает копию памяти родителя вместе со всеми блокировками в том
состоянии, в каком они были в момент `fork`. В CLI родитель однопоточный, и
это безопасно. В вебе родитель — uvicorn с пулом потоков: если в момент `fork`
какой-то поток держал блокировку (логирование, аллокатор, GEOS), ребёнок
получает её запертой навсегда и ждёт вечно.

Живьём: загрузка «9. Измайловская площадь» через веб — дочерний процесс в
`futex_do_wait`, один поток, 2,1 ГБ памяти, ноль процессора, задача «в работе»
без конца. Тот же код из CLI отрабатывает за полторы минуты.

`forkserver` порождает процессы от отдельного чистого однопоточного сервера,
поэтому унаследованных блокировок нет; функции и аргументы и так передаются
пиклингом (это уже требование к коду в planner.py и dxf_reader.py). На
системах без `forkserver` (Windows, macOS по умолчанию spawn) берётся
стандартный способ.
"""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor


def _context():
    if "forkserver" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("forkserver")
    return None


def process_pool(max_workers: int | None = None) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=max_workers, mp_context=_context())
