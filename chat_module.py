"""Messages (3.47.0) — private conversations between users, notes to self
and reminders, behind the floating inbox button (base.html + static/js/chat.js).

SANDBOXED like find_module / security_module: never imports app.py; ONE
`chat_module.register(app, **deps)` call wires it in. It owns three tables
(chat_threads, chat_participants, chat_messages) and writes to exactly one
main-app table: `show_comments`, and only for a conversation its
participants switched to "also post to the show's comments".

The model
- A conversation (thread) = a set of people + an optional tagged show.
  `member_key` is the sorted participant ids; sending to the same people
  about the same show always lands in the same thread, so "sort threads by
  show" is just grouping on show_id. A thread whose only member is the
  sender is that person's "Notes to self".
- PRIVATE by default: only participants can read a thread — every route
  checks chat_participants; admins get no bypass and nothing here is
  indexed by Search / #tags (find_module) or written to the audit log's
  before/after columns.
- `post_to_comments` (thread level, needs a show): while on, every message
  sent in the thread — by anyone in it — is ALSO inserted into that show's
  comments (show_comments.chat_thread_id marks where it came from), where
  everyone with access to the show can read it. Nobody may be caught off
  guard by that, so:
    * switching it on/off is its own action and writes a system line into
      the thread that every participant sees (and that counts as unread);
    * each send carries what the SENDER'S SCREEN said (`post_to_comments`);
      when it disagrees with the thread the send is refused with 409 and
      the client re-renders — a stale tab can never publish a message the
      writer believed was private (or keep one private that the thread now
      publishes);
    * read-only accounts can't post comments, so they can't send while a
      thread posts to comments.
  Messages sent before it was switched on stay private; a comment posted
  this way is an ordinary comment afterwards (its author can edit/delete it
  on the show — that never touches the message).
- Reminders: a message of your own may carry `remind_at` (TIMESTAMPTZ, an
  absolute instant, compared with NOW()). Once due it counts in the inbox
  badge until its author presses Done. No background job: the badge poll
  computes it. A reminder is private to its author — other participants
  never receive remind_at.
- Email (app.py run_inbox_emails, leader-gated): pending_email_items()
  hands the job each user's messages left unread for a while and reminders
  that came due, stamping them so each is mailed once. Whether a user wants
  them is user_notify_prefs (My Account). Those emails are `sensitive`
  (never stored in the email log — they quote private messages).
- Unread = messages from others with id > chat_participants.last_read_id.
  Reading is an explicit POST (…/read); every GET here is side-effect free
  (hover prefetch already skips /api/*).

No websockets (the app has none): chat.js polls /api/chat/status every
30 s for the badge and the open thread every 5 s, paused while the tab is
hidden.
"""

import re
from datetime import datetime, timedelta, timezone

from flask import abort, jsonify, request, session

MODULE_KEY = 'messages'
BODY_MAX = 2000              # = the show-comment limit (a public message becomes one)
MAX_RECIPIENTS = 20
SEND_PER_MINUTE = 30         # per user, all conversations (a runaway script / paste loop)
THREAD_LIST_MAX = 300
PAGE = 60                    # messages per page of a thread
REMIND_MAX_DAYS = 366 * 2

# A user who can be written to / can write: an approved, unlocked account
# that isn't a document viewer (viewers are confined to /viewer and could
# never open the inbox). 321Theater flags only — never is_app_*.
_ACTIVE_USER_SQL = ("COALESCE(u.is_locked, 0) = 0 AND COALESCE(u.pending_approval, 0) = 0 "
                    "AND COALESCE(u.is_document_viewer, 0) = 0")
_NAME_SQL = "COALESCE(NULLIF(TRIM(u.display_name), ''), u.username)"


def pending_email_items(db, delay_minutes):
    """What the inbox email job should mail right now (3.47.0), and stamp it
    so it is mailed once: {user_id: {'messages': [...], 'reminders': [...]}}.
    - messages: a participant's unread messages from others, once the OLDEST
      of them has sat unread for `delay_minutes` (someone reading along in
      the app is never emailed); everything unread in that thread goes in
      the one email and chat_participants.emailed_up_to_id moves past it.
    - reminders: the user's own reminders that have come due.
    Preferences / addresses are the caller's business. Caller commits."""
    out = {}
    # Claims are ATOMIC: am_i_leader() is true in every worker when the
    # leader is forced or the heartbeat is off, so several can run this at
    # once. Participant rows are locked (SKIP LOCKED: a second runner passes
    # over what the first holds) and reminders are stamped by the one
    # statement that returns them — nothing is mailed twice.
    claim = db.execute(
        """SELECT p.thread_id, p.user_id,
                  GREATEST(p.last_read_id, COALESCE(p.emailed_up_to_id, 0)) AS seen
             FROM chat_participants p
            WHERE EXISTS (SELECT 1 FROM chat_messages o
                           WHERE o.thread_id = p.thread_id
                             AND o.id > GREATEST(p.last_read_id, COALESCE(p.emailed_up_to_id, 0))
                             AND o.user_id IS DISTINCT FROM p.user_id
                             AND o.created_at <= NOW() - %s * INTERVAL '1 minute')
            ORDER BY p.thread_id, p.user_id LIMIT 500
              FOR UPDATE OF p SKIP LOCKED""", (delay_minutes,)).fetchall()
    for c in claim:
        rows = db.execute(
            """SELECT m.thread_id, m.id, m.author_name, m.body, m.kind,
                      m.created_at::timestamptz AS created_at,
                      t.show_id, s.name AS show_name, t.post_to_comments
                 FROM chat_messages m
                 JOIN chat_threads t ON t.id = m.thread_id
                 LEFT JOIN shows s ON s.id = t.show_id
                WHERE m.thread_id = %s AND m.id > %s AND m.user_id IS DISTINCT FROM %s
                ORDER BY m.id LIMIT 200""", (c['thread_id'], c['seen'], c['user_id'])).fetchall()
        if not rows:
            continue
        out.setdefault(c['user_id'], {'messages': [], 'reminders': []})['messages'] += [
            dict(r, user_id=c['user_id']) for r in rows]
        db.execute('UPDATE chat_participants SET emailed_up_to_id = %s '
                   'WHERE thread_id = %s AND user_id = %s',
                   (rows[-1]['id'], c['thread_id'], c['user_id']))
    due = db.execute(
        """WITH c AS (UPDATE chat_messages SET remind_emailed_at = CURRENT_TIMESTAMP
                       WHERE remind_at IS NOT NULL AND remind_at <= NOW()
                         AND remind_done_at IS NULL AND remind_emailed_at IS NULL
                         AND user_id IS NOT NULL
                   RETURNING id, user_id, thread_id, body, remind_at)
           SELECT c.id, c.user_id, c.thread_id, c.body, c.remind_at, s.name AS show_name
             FROM c JOIN chat_threads t ON t.id = c.thread_id
             LEFT JOIN shows s ON s.id = t.show_id
            ORDER BY c.remind_at""").fetchall()
    for r in due:
        out.setdefault(r['user_id'], {'messages': [], 'reminders': []})['reminders'].append(dict(r))
    return out


def register(app, *, get_db, login_required, ts_out, log_audit, syslog_logger,
             module_enabled, can_access_show, notify_mentions=None):

    def _on():
        return module_enabled(MODULE_KEY)

    def _guard():
        """404 while the module is off; the signed-in user's id."""
        if not _on():
            abort(404)
        return session['user_id']

    def _err(msg, code=400, **extra):
        return jsonify(dict({'success': False, 'error': msg}, **extra)), code

    def _can_comment():
        # The show-comment rule (post_comment): read-only / restricted can't.
        return not (session.get('is_readonly') or session.get('is_restricted'))

    def _parse_remind(v):
        """(aware datetime | None, error). Accepts an ISO instant with offset
        ('…Z' included) — the browser sends its local pick as UTC."""
        if v in (None, ''):
            return None, None
        try:
            dt = datetime.fromisoformat(str(v).strip().replace('Z', '+00:00'))
        except ValueError:
            return None, 'Unreadable reminder time.'
        if dt.tzinfo is None:
            return None, 'Unreadable reminder time.'
        now = datetime.now(timezone.utc)
        if dt < now - timedelta(minutes=2):
            return None, 'Pick a reminder time in the future.'
        if dt > now + timedelta(days=REMIND_MAX_DAYS):
            return None, 'That reminder is too far away (two years at most).'
        return dt, None

    def _clean_body(v):
        body = str(v or '').replace('\r\n', '\n').replace('\x00', '').strip()
        if not body:
            return None, 'Write a message first.'
        if len(body) > BODY_MAX:
            return None, f'Message too long (max {BODY_MAX} characters).'
        return body, None

    def _member(db, tid, uid, lock=False):
        """The thread row (+ my last_read_id) when `uid` is in it, else None."""
        return db.execute(
            f"""SELECT t.*, p.last_read_id, s.name AS show_name, s.status AS show_status
                  FROM chat_threads t
                  JOIN chat_participants p ON p.thread_id = t.id AND p.user_id = %s
                  LEFT JOIN shows s ON s.id = t.show_id
                 WHERE t.id = %s {'FOR UPDATE OF t' if lock else ''}""",
            (uid, tid)).fetchone()

    def _members(db, tids):
        """{thread_id: [{id, name}]} — current participants, by name."""
        out = {t: [] for t in tids}
        if not tids:
            return out
        for r in db.execute(
                f"""SELECT p.thread_id, u.id, {_NAME_SQL} AS name
                      FROM chat_participants p JOIN users u ON u.id = p.user_id
                     WHERE p.thread_id = ANY(%s) ORDER BY LOWER({_NAME_SQL}), u.id""",
                (list(tids),)).fetchall():
            out[r['thread_id']].append({'id': r['id'], 'name': r['name']})
        return out

    def _msg_out(r, uid):
        mine = r['user_id'] is not None and r['user_id'] == uid
        d = {'id': r['id'], 'kind': r['kind'] or 'message', 'body': r['body'],
             'author': r['author_name'] or 'Former user', 'author_id': r['user_id'],
             'mine': mine, 'created_at': ts_out(r['created_at']),
             'in_comments': bool(r['comment_id'])}
        if mine and r['remind_at'] is not None:      # private to the author
            d['remind_at'] = ts_out(r['remind_at'])
            d['remind_done'] = r['remind_done_at'] is not None
            d['remind_due'] = (not d['remind_done']
                               and r['remind_at'] <= datetime.now(timezone.utc))
        return d

    _MSG_COLS = ("m.id, m.thread_id, m.user_id, m.author_name, m.kind, m.body, m.comment_id, "
                 "m.created_at::timestamptz AS created_at, m.remind_at, m.remind_done_at")

    def _thread_out(db, t, uid):
        mem = _members(db, [t['id']])[t['id']]
        return {'id': t['id'], 'members': mem,
                # From the thread's own key: a conversation whose other
                # person's account was deleted is NOT a note to self.
                'is_self': (t['member_key'] or '') == str(uid),
                'show': ({'id': t['show_id'], 'name': t['show_name'],
                          'status': t['show_status'] or 'active'} if t['show_id'] else None),
                'post_to_comments': bool(t['post_to_comments']) and bool(t['show_id']),
                'can_comment': _can_comment()}

    def _insert_message(db, t, uid, name, body, *, kind='message', remind_at=None):
        """Insert one message (and its show comment while the thread posts to
        comments); bump the thread. Returns the message id. Caller commits."""
        comment_id = None
        if kind == 'message' and t['post_to_comments'] and t['show_id']:
            comment_id = db.execute(
                'INSERT INTO show_comments (show_id, user_id, author_name, body, chat_thread_id) '
                'VALUES (%s, %s, %s, %s, %s) RETURNING id',
                (t['show_id'], uid, name, body, t['id'])).fetchone()['id']
            # Same audit action as a comment typed on the show.
            log_audit(db, 'COMMENT_POST', 'comment', comment_id, show_id=t['show_id'],
                      after={'body': body}, detail=f"via=message thread={t['id']}")
            if notify_mentions:      # an @name in a public message works like in a comment
                notify_mentions(db, t['show_id'], body, uid, name, comment_id=comment_id)
        mid = db.execute(
            'INSERT INTO chat_messages (thread_id, user_id, author_name, kind, body, comment_id, remind_at) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (t['id'], uid, name, kind, body, comment_id, remind_at)).fetchone()['id']
        db.execute('UPDATE chat_threads SET last_message_at = CURRENT_TIMESTAMP, last_message_id = %s '
                   'WHERE id = %s', (mid, t['id']))
        # The sender has read their own message.
        db.execute('UPDATE chat_participants SET last_read_id = %s '
                   'WHERE thread_id = %s AND user_id = %s AND last_read_id < %s',
                   (mid, t['id'], uid, mid))
        return mid

    def _too_fast(db, uid):
        n = db.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE user_id = %s "
                       "AND kind = 'message' AND created_at > NOW() - INTERVAL '1 minute'",
                       (uid,)).fetchone()['n']
        return n >= SEND_PER_MINUTE

    def _my_name(db, uid):
        r = db.execute(f'SELECT {_NAME_SQL} AS name FROM users u WHERE u.id = %s', (uid,)).fetchone()
        return (r['name'] if r else '') or session.get('username') or 'User'

    def _set_comments_mode(db, t, uid, name, on):
        """Flip a thread's post-to-comments switch and leave the system line
        every participant sees. Returns the system message id."""
        db.execute('UPDATE chat_threads SET post_to_comments = %s WHERE id = %s',
                   (1 if on else 0, t['id']))
        text = (f'{name} turned ON posting to show comments. From here on, every message in this '
                f'conversation is also posted to the comments of “{t["show_name"]}”, where anyone '
                'with access to the show can read it. Earlier messages stay private.'
                if on else
                f'{name} turned OFF posting to show comments. Messages from here on are private '
                'to this conversation. Comments already posted stay on the show.')
        sys_t = dict(t)
        sys_t['post_to_comments'] = 0          # a system line is never a comment
        mid = _insert_message(db, sys_t, uid, name, text, kind='system')
        log_audit(db, 'CHAT_COMMENTS_MODE', 'chat_thread', t['id'], show_id=t['show_id'],
                  detail=f"post_to_comments={'on' if on else 'off'}")
        return mid

    # ── status: the badge poll (one cheap statement, no writes) ──────────────
    @app.route('/api/chat/status')
    @login_required
    def api_chat_status():
        uid = session['user_id']
        db = get_db()
        try:
            if _on():
                r = db.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM chat_participants p
                            JOIN chat_messages m ON m.thread_id = p.thread_id
                                 AND m.id > p.last_read_id AND m.user_id IS DISTINCT FROM p.user_id
                           WHERE p.user_id = %s) AS unread,
                         (SELECT COUNT(*) FROM chat_messages m
                           WHERE m.user_id = %s AND m.remind_at IS NOT NULL
                             AND m.remind_done_at IS NULL AND m.remind_at <= NOW()) AS reminders,
                         (SELECT COALESCE(MAX(t.last_message_id), 0) FROM chat_participants p
                            JOIN chat_threads t ON t.id = p.thread_id WHERE p.user_id = %s) AS latest,
                         (SELECT COUNT(*) FROM notifications
                           WHERE user_id = %s AND read_at IS NULL) AS notifications""",
                    (uid, uid, uid, uid)).fetchone()
                out = {'enabled': True, 'unread': int(r['unread'] or 0),
                       'reminders': int(r['reminders'] or 0), 'latest': int(r['latest'] or 0),
                       'notifications': int(r['notifications'] or 0)}
            else:
                r = db.execute('SELECT COUNT(*) AS n FROM notifications '
                               'WHERE user_id = %s AND read_at IS NULL', (uid,)).fetchone()
                out = {'enabled': False, 'unread': 0, 'reminders': 0, 'latest': 0,
                       'notifications': int(r['n'] or 0)}
        finally:
            db.close()
        return jsonify(out)

    # ── people / shows pickers ───────────────────────────────────────────────
    @app.route('/api/chat/people')
    @login_required
    def api_chat_people():
        uid = _guard()
        db = get_db()
        try:
            rows = db.execute(
                f"""SELECT u.id, {_NAME_SQL} AS name, u.role
                      FROM users u WHERE {_ACTIVE_USER_SQL} AND u.id <> %s
                     ORDER BY LOWER({_NAME_SQL}), u.id""", (uid,)).fetchall()
        finally:
            db.close()
        return jsonify({'people': [{'id': r['id'], 'name': r['name'], 'role': r['role'] or ''}
                                   for r in rows]})

    @app.route('/api/chat/shows')
    @login_required
    def api_chat_shows():
        """Active shows / events a message can be tagged with (newest dates
        first within upcoming; `q` narrows by name or venue)."""
        _guard()
        q = (request.args.get('q') or '').strip()[:80]
        like = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        db = get_db()
        try:
            rows = db.execute(
                """SELECT s.id, s.name, s.venue,
                          COALESCE(s.show_date, s.load_in_date) AS d
                     FROM shows s
                    WHERE s.status = 'active'
                      AND (%s = '' OR s.name ILIKE %s OR COALESCE(s.venue, '') ILIKE %s)
                    ORDER BY (COALESCE(s.show_date, s.load_in_date) IS NULL),
                             (COALESCE(s.show_date, s.load_in_date) < CURRENT_DATE),
                             ABS(COALESCE(s.show_date, s.load_in_date) - CURRENT_DATE), s.name
                    LIMIT 40""", (q, like, like)).fetchall()
        finally:
            db.close()
        uid = session['user_id']
        return jsonify({'shows': [
            {'id': r['id'], 'name': r['name'], 'venue': (r['venue'] or '').strip(),
             'date': r['d'].isoformat() if r['d'] else ''}
            for r in rows if can_access_show(uid, r['id'])]})

    # ── thread list ──────────────────────────────────────────────────────────
    @app.route('/api/chat/threads')
    @login_required
    def api_chat_threads():
        uid = _guard()
        db = get_db()
        try:
            rows = db.execute(
                """SELECT t.id, t.show_id, t.post_to_comments, t.last_message_id, t.member_key,
                          t.last_message_at::timestamptz AS last_at,
                          s.name AS show_name, s.status AS show_status,
                          COALESCE(s.show_date, s.load_in_date) AS show_date,
                          (SELECT COUNT(*) FROM chat_messages m
                            WHERE m.thread_id = t.id AND m.id > p.last_read_id
                              AND m.user_id IS DISTINCT FROM p.user_id) AS unread,
                          (SELECT COUNT(*) FROM chat_messages m
                            WHERE m.thread_id = t.id AND m.user_id = p.user_id
                              AND m.remind_at IS NOT NULL AND m.remind_done_at IS NULL
                              AND m.remind_at <= NOW()) AS reminders,
                          lm.body AS last_body, lm.user_id AS last_user,
                          lm.author_name AS last_author, lm.kind AS last_kind
                     FROM chat_participants p
                     JOIN chat_threads t ON t.id = p.thread_id
                     LEFT JOIN shows s ON s.id = t.show_id
                     LEFT JOIN LATERAL (SELECT m.body, m.user_id, m.author_name, m.kind
                                          FROM chat_messages m WHERE m.thread_id = t.id
                                         ORDER BY m.id DESC LIMIT 1) lm ON TRUE
                    WHERE p.user_id = %s
                    ORDER BY t.last_message_at DESC, t.id DESC LIMIT %s""",
                (uid, THREAD_LIST_MAX)).fetchall()
            members = _members(db, [r['id'] for r in rows])
        finally:
            db.close()
        out = []
        for r in rows:
            mem = members.get(r['id'], [])
            body = (r['last_body'] or '')
            out.append({
                'id': r['id'], 'members': mem,
                'is_self': (r['member_key'] or '') == str(uid),
                'show': ({'id': r['show_id'], 'name': r['show_name'],
                          'status': r['show_status'] or 'active',
                          'date': r['show_date'].isoformat() if r['show_date'] else ''}
                         if r['show_id'] else None),
                'post_to_comments': bool(r['post_to_comments']) and bool(r['show_id']),
                'unread': int(r['unread'] or 0), 'reminders': int(r['reminders'] or 0),
                'last_at': ts_out(r['last_at']), 'last_id': r['last_message_id'] or 0,
                'last': {'body': body[:160] + ('…' if len(body) > 160 else ''),
                         'mine': r['last_user'] == uid, 'author': r['last_author'] or '',
                         'kind': r['last_kind'] or 'message'} if r['last_kind'] else None})
        return jsonify({'threads': out})

    # ── start (or continue) a conversation ───────────────────────────────────
    @app.route('/api/chat/threads', methods=['POST'])
    @login_required
    def api_chat_start():
        """Send a message to a set of people (none = a note to self),
        optionally tagged with an active show. The same people + show reuse
        their existing thread."""
        uid = _guard()
        data = request.get_json(silent=True) or {}
        body, err = _clean_body(data.get('body'))
        if err:
            return _err(err)
        remind_at, err = _parse_remind(data.get('remind_at'))
        if err:
            return _err(err)
        try:
            others = sorted({int(x) for x in (data.get('user_ids') or [])} - {uid})
            show_id = int(data['show_id']) if data.get('show_id') not in (None, '') else None
        except (TypeError, ValueError):
            return _err('Unreadable recipients.')
        if len(others) > MAX_RECIPIENTS:
            return _err(f'A conversation can have at most {MAX_RECIPIENTS} other people.')
        want_public = bool(data.get('post_to_comments'))
        if want_public and not show_id:
            return _err('Tag a show to post the message to its comments.')
        if want_public and not _can_comment():
            return _err('Your account is read-only, so it cannot post to show comments.', 403)
        db = get_db()
        try:
            if _too_fast(db, uid):
                return _err('You are sending messages very quickly — wait a minute.', 429)
            if others:
                ok = {r['id'] for r in db.execute(
                    f'SELECT u.id FROM users u WHERE u.id = ANY(%s) AND {_ACTIVE_USER_SQL}',
                    (others,)).fetchall()}
                if ok != set(others):
                    return _err('One of those people can no longer receive messages.', 409)
            show = None
            if show_id is not None:
                show = db.execute('SELECT id, name, status FROM shows WHERE id = %s',
                                  (show_id,)).fetchone()
                if not show or not can_access_show(uid, show_id):
                    return _err('Show not found.', 404)
            key = ','.join(str(i) for i in sorted(set(others) | {uid}))
            # Two people starting the same conversation at once must end up in
            # ONE thread (transaction-scoped lock; nothing session-level).
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (f'chat:{key}:{show_id}',))
            t = db.execute(
                """SELECT t.*, s.name AS show_name, s.status AS show_status
                     FROM chat_threads t LEFT JOIN shows s ON s.id = t.show_id
                    WHERE t.member_key = %s AND t.show_id IS NOT DISTINCT FROM %s
                      AND EXISTS (SELECT 1 FROM chat_participants p
                                   WHERE p.thread_id = t.id AND p.user_id = %s)
                    ORDER BY (COALESCE(t.post_to_comments, 0) = %s) DESC, t.id
                    LIMIT 1 FOR UPDATE OF t""",
                (key, show_id, uid, 1 if want_public else 0)).fetchone()
            # A show merge / delete can leave two threads with the same
            # people + show (nothing here is unique on purpose): prefer the
            # one already in the mode the writer asked for, else the oldest.
            name = _my_name(db, uid)
            created = False
            if t is None:
                if show and show['status'] != 'active':
                    return _err('Only an active show or event can be tagged on a new conversation.', 409)
                tid = db.execute(
                    'INSERT INTO chat_threads (show_id, member_key, post_to_comments, created_by) '
                    'VALUES (%s, %s, %s, %s) RETURNING id',
                    (show_id, key, 1 if want_public else 0, uid)).fetchone()['id']
                db.executemany('INSERT INTO chat_participants (thread_id, user_id) VALUES (%s, %s)',
                               [(tid, u) for u in sorted(set(others) | {uid})])
                t = db.execute(
                    """SELECT t.*, s.name AS show_name, s.status AS show_status
                         FROM chat_threads t LEFT JOIN shows s ON s.id = t.show_id
                        WHERE t.id = %s""", (tid,)).fetchone()
                created = True
            elif bool(t['post_to_comments']) and t['show_id'] and not want_public:
                # The writer believes this is private; the thread publishes.
                db.rollback()
                return _err('Your conversation with these people about this show already posts '
                            'to the show’s comments. Open it to continue there, or turn that '
                            'off first.', 409, thread_id=t['id'], reason='thread_is_public')
            elif want_public and not t['post_to_comments']:
                _set_comments_mode(db, t, uid, name, True)
                t = dict(t)
                t['post_to_comments'] = 1
            mid = _insert_message(db, t, uid, name, body, remind_at=remind_at)
            db.commit()
        finally:
            db.close()
        syslog_logger.info(
            f"CHAT_SEND thread={t['id']} msg={mid} new_thread={int(created)} "
            f"show_id={show_id or '-'} to_comments={int(bool(t['post_to_comments']))} "
            f"recipients={len(others)} reminder={int(remind_at is not None)} "
            f"by={session.get('username')}")
        return jsonify({'success': True, 'thread_id': t['id'], 'message_id': mid, 'created': created})

    # ── one thread ───────────────────────────────────────────────────────────
    @app.route('/api/chat/threads/<int:tid>')
    @login_required
    def api_chat_thread(tid):
        """Thread + messages. `after=<id>` → only newer ones (the 5 s poll);
        `before=<id>` → the page before (scroll back); neither → the newest
        page. Never writes (reading is POST …/read)."""
        uid = _guard()
        after = request.args.get('after', type=int)
        before = request.args.get('before', type=int)
        db = get_db()
        try:
            t = _member(db, tid, uid)
            if not t:
                return _err('Conversation not found.', 404)
            if after is not None:
                rows = db.execute(
                    f'SELECT {_MSG_COLS} FROM chat_messages m WHERE m.thread_id = %s AND m.id > %s '
                    'ORDER BY m.id LIMIT 500', (tid, after)).fetchall()
                more = False
            else:
                cond, args = ('AND m.id < %s', [before]) if before else ('', [])
                rows = db.execute(
                    f'SELECT {_MSG_COLS} FROM chat_messages m WHERE m.thread_id = %s {cond} '
                    'ORDER BY m.id DESC LIMIT %s', [tid] + args + [PAGE + 1]).fetchall()
                more = len(rows) > PAGE
                rows = list(reversed(rows[:PAGE]))
            out = _thread_out(db, t, uid)
            out['last_read_id'] = t['last_read_id'] or 0
            # My reminders that came due on messages the page may not hold.
            due = db.execute(
                f"""SELECT {_MSG_COLS} FROM chat_messages m
                     WHERE m.thread_id = %s AND m.user_id = %s AND m.remind_at IS NOT NULL
                       AND m.remind_done_at IS NULL AND m.remind_at <= NOW()
                     ORDER BY m.remind_at LIMIT 20""", (tid, uid)).fetchall()
        finally:
            db.close()
        return jsonify({'thread': out, 'messages': [_msg_out(r, uid) for r in rows],
                        'more': more, 'due': [_msg_out(r, uid) for r in due]})

    @app.route('/api/chat/threads/<int:tid>/read', methods=['POST'])
    @login_required
    def api_chat_read(tid):
        uid = _guard()
        data = request.get_json(silent=True) or {}
        try:
            up_to = int(data.get('up_to') or 0)
        except (TypeError, ValueError):
            up_to = 0
        db = get_db()
        try:
            # Never past the thread's real last message (a client can't mark
            # messages that don't exist yet as read).
            db.execute(
                """UPDATE chat_participants p SET last_read_id = LEAST(%s, t.last_message_id)
                     FROM chat_threads t
                    WHERE t.id = p.thread_id AND p.thread_id = %s AND p.user_id = %s
                      AND p.last_read_id < LEAST(%s, t.last_message_id)""",
                (up_to, tid, uid, up_to))
            db.commit()
        finally:
            db.close()
        return jsonify({'success': True})

    @app.route('/api/chat/threads/<int:tid>/messages', methods=['POST'])
    @login_required
    def api_chat_send(tid):
        uid = _guard()
        data = request.get_json(silent=True) or {}
        body, err = _clean_body(data.get('body'))
        if err:
            return _err(err)
        remind_at, err = _parse_remind(data.get('remind_at'))
        if err:
            return _err(err)
        db = get_db()
        try:
            t = _member(db, tid, uid, lock=True)
            if not t:
                return _err('Conversation not found.', 404)
            if _too_fast(db, uid):
                return _err('You are sending messages very quickly — wait a minute.', 429)
            public = bool(t['post_to_comments']) and bool(t['show_id'])
            # What the sender's screen said. A mismatch means the switch moved
            # (or this tab is stale): refuse, the client re-reads and asks.
            if 'post_to_comments' not in data or bool(data.get('post_to_comments')) != public:
                db.rollback()
                return _err('This conversation now posts to the show’s comments — check the banner, '
                            'then send again.' if public else
                            'This conversation no longer posts to the show’s comments — your message '
                            'would stay private. Check, then send again.',
                            409, reason='comments_mode_changed', post_to_comments=public)
            if public and not _can_comment():
                db.rollback()
                return _err('This conversation posts to the show’s comments and your account is '
                            'read-only, so you can’t send here.', 403)
            mid = _insert_message(db, t, uid, _my_name(db, uid), body, remind_at=remind_at)
            db.commit()
        finally:
            db.close()
        syslog_logger.info(
            f"CHAT_SEND thread={tid} msg={mid} new_thread=0 show_id={t['show_id'] or '-'} "
            f"to_comments={int(public)} reminder={int(remind_at is not None)} "
            f"by={session.get('username')}")
        return jsonify({'success': True, 'message_id': mid, 'in_comments': public})

    @app.route('/api/chat/threads/<int:tid>/comments-mode', methods=['POST'])
    @login_required
    def api_chat_comments_mode(tid):
        """Switch "also post to the show's comments" on or off for a thread
        (any participant; needs a tagged show)."""
        uid = _guard()
        on = bool((request.get_json(silent=True) or {}).get('on'))
        db = get_db()
        try:
            t = _member(db, tid, uid, lock=True)
            if not t:
                return _err('Conversation not found.', 404)
            if not t['show_id']:
                return _err('This conversation has no show tagged, so there are no comments to post to.', 409)
            if on and not _can_comment():
                return _err('Your account is read-only, so it cannot post to show comments.', 403)
            if bool(t['post_to_comments']) != on:
                _set_comments_mode(db, t, uid, _my_name(db, uid), on)
                db.commit()
                syslog_logger.info(f"CHAT_COMMENTS_MODE thread={tid} show_id={t['show_id']} "
                                   f"on={int(on)} by={session.get('username')}")
        finally:
            db.close()
        return jsonify({'success': True, 'post_to_comments': on})

    # ── reminders ────────────────────────────────────────────────────────────
    @app.route('/api/chat/messages/<int:mid>/reminder', methods=['POST'])
    @login_required
    def api_chat_reminder(mid):
        """Set / move / clear the reminder on one of MY messages, or mark a
        due one done: {remind_at: iso|null} or {done: true}."""
        uid = _guard()
        data = request.get_json(silent=True) or {}
        db = get_db()
        try:
            m = db.execute(
                """SELECT m.id FROM chat_messages m
                     JOIN chat_participants p ON p.thread_id = m.thread_id AND p.user_id = %s
                    WHERE m.id = %s AND m.user_id = %s AND m.kind = 'message' FOR UPDATE OF m""",
                (uid, mid, uid)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            if data.get('done'):
                db.execute('UPDATE chat_messages SET remind_done_at = CURRENT_TIMESTAMP '
                           'WHERE id = %s AND remind_at IS NOT NULL AND remind_done_at IS NULL', (mid,))
            else:
                remind_at, err = _parse_remind(data.get('remind_at'))
                if err:
                    return _err(err)
                db.execute('UPDATE chat_messages SET remind_at = %s, remind_done_at = NULL, '
                           'remind_emailed_at = NULL WHERE id = %s', (remind_at, mid))
            db.commit()
            row = db.execute(f'SELECT {_MSG_COLS} FROM chat_messages m WHERE m.id = %s',
                             (mid,)).fetchone()
        finally:
            db.close()
        return jsonify({'success': True, 'message': _msg_out(row, uid)})

    @app.context_processor
    def _inject_chat():
        # Called by base.html only for a signed-in, non-viewer page (one
        # settings read), so it costs nothing on the sign-in pages.
        return {'chat_enabled': _on}
