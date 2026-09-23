import argparse
import getpass
import json
import os
from unittest.mock import patch

from jev_ultrafast import model

parser = argparse.ArgumentParser()
parser.add_argument('--live', action='store_true')
args = parser.parse_args()
state = {
    'url': 'https://example.invalid/reading',
    'title': 'Synthetic reading room',
    'text': 'Choose an article: Baking bread; Browser automation with finite choices.',
    'actions': [
        {'id': 'bread', 'kind': 'click', 'node': 1, 'role': 'link', 'label': 'Baking bread'},
        {'id': 'browser', 'kind': 'click', 'node': 2, 'role': 'link',
         'label': 'Browser automation with finite choices'},
    ],
}
goal = 'Open the article about browser automation with finite choices.'

def synthetic_response(_url, _key, body):
    answers = {}
    for name, question in body['questions'].items():
        selected = 'CLICK' if name == 'operation' else '2'
        answers[name] = {
            'choice': selected, 'confidence': 1.0,
            'probabilities': {key: float(key == selected) for key in question['criteria']},
        }
    return {'model': 'synthetic-test-engine', 'answers': answers}

if args.live:
    if not os.environ.get('TYPESAFE_API_KEY'):
        os.environ['TYPESAFE_API_KEY'] = getpass.getpass('TypeSafe key (hidden): ')
    os.environ.setdefault('TYPESAFE_MODEL', 'jev-1.13.0')
    result = model.choose(state, goal, [])  # One decision; up to 3 HTTP attempts.
else:
    with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-unused'}):
        with patch.object(model, 'post_json', synthetic_response):
            result = model.choose(state, goal, [])
    assert (result['operation'], result['choice']) == ('CLICK', 'browser')

print(json.dumps({
    'mode': 'live' if args.live else 'synthetic',
    'operation': result['operation'], 'action_id': result['choice'],
    'raw_answers': result['raw_answers'], 'browser_actions': 0,
}, indent=2))
