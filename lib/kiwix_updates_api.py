"""Flask blueprint: ZIM update API (see lib/zim_updater.py)."""
import re

from flask import Blueprint, jsonify, request

from . import zim_updater as zu

kiwix_updates_bp = Blueprint('kiwix_updates', __name__)


def _started(run_id, err):
    if err:
        return jsonify({'error': err}), 409
    return jsonify({'ok': True, 'run_id': run_id}), 202


@kiwix_updates_bp.route('/api/kiwix/updates')
def api_updates():
    return jsonify(zu.list_updates())


@kiwix_updates_bp.route('/api/kiwix/updates/check', methods=['POST'])
def api_updates_check_all():
    return jsonify({'ok': True, 'started': zu.check_all_async()}), 202


@kiwix_updates_bp.route('/api/kiwix/updates/<key>/check', methods=['POST'])
def api_updates_check(key):
    try:
        row = zu.check_key(key)
    except zu.StepError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True, 'catalog': row})


@kiwix_updates_bp.route('/api/kiwix/updates/<key>/update', methods=['POST'])
def api_updates_update(key):
    return _started(*zu.start_update(key, 'manual'))


@kiwix_updates_bp.route('/api/kiwix/updates/<key>/rollback', methods=['POST'])
def api_updates_rollback(key):
    return _started(*zu.start_rollback(key, 'manual'))


@kiwix_updates_bp.route('/api/kiwix/updates/<key>/recrawl', methods=['POST'])
def api_updates_recrawl(key):
    return _started(*zu.start_recrawl(key, 'manual'))


@kiwix_updates_bp.route('/api/kiwix/updates/<key>/schedule', methods=['PUT'])
def api_updates_schedule(key):
    try:
        s = zu.set_schedule(key, request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'schedule': s})


@kiwix_updates_bp.route('/api/kiwix/updates/calendar')
def api_updates_calendar():
    month = request.args.get('month') or ''
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month):
        return jsonify({'error': 'month must be YYYY-MM'}), 400
    return jsonify(zu.calendar(month))


@kiwix_updates_bp.route('/api/kiwix/updates/runs/<int:run_id>')
def api_updates_run(run_id):
    r = zu.get_run(run_id)
    if not r:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify(r)
