-- Галочка «Матч за 3-е место» в редакторе сетки: 1 - разыгрывается (как раньше), 0 - нет.
ALTER TABLE tournaments ADD COLUMN third_place_enabled INTEGER NOT NULL DEFAULT 1;
