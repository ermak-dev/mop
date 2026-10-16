"""Чистое сравнение LDAP group DN; без framework и provider I/O."""


def directory_dn_key(dn):
    def normalized(part):
        start, end = 0, len(part)
        while start < end and part[start][0].isspace() and not part[start][1]:
            start += 1
        while end > start and part[end - 1][0].isspace() and not part[end - 1][1]:
            end -= 1
        return ''.join(char for char, _ in part[start:end]).casefold()

    result, part, escaped = [], [], False
    for char in dn:
        if char == ',' and not escaped:
            result.append(normalized(part))
            part = []
            continue
        part.append((char, escaped))
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
    result.append(normalized(part))
    return ','.join(result)
