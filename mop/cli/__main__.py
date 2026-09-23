"""`python3 -m mop.cli <команда>`: то же, что `mop <команда>`, из любого
каталога и без симлинка — так может звать и спека, и юнит."""
import sys

from . import main

sys.exit(main(sys.argv[1:]))
