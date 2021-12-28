forbidden_characters = [' ', '`', '~', '!', '@', '#', '$', '%', '^', '&', '*', '(', ')', '-', '_', '=', '+', '{', '}',
                        '[', ']', '|', '\\', ':', ';', '"', '\'', '<', '>', ',', '.', '?', '/']


def sanitize_nickname(nickname):
    return ''.join(filter(is_acceptable_character, nickname))[:16]


def is_acceptable_character(c):
    o = ord(c)
    return 65 <= o <= 90 or 97 <= o <= 122 or 48 <= o <= 57 or 44032 <= o <= 55203 or c in ['_', '-', '.']
