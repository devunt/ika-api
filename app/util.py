def sanitize_nickname(nickname):
    return nickname.replace(' ', '_').replace('!', 'ǃ').replace('@', '＠')
