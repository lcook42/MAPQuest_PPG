"""Session-local search hints. Historical windows never qualify a new baseline."""
from collections import defaultdict
from copy import deepcopy


class CalibrationHistory:
    def __init__(self):
        self.attempts = []

    def add(self, results, status, reason='', brightness=None):
        attempt = dict(id=len(self.attempts)+1, included=True, status=status,
                       reason=reason, results=deepcopy(results), brightness=deepcopy(brightness or []))
        self.attempts.append(attempt)
        return attempt

    def remove(self, attempt_id):
        attempt = next((a for a in self.attempts if a['id'] == attempt_id and a['included']), None)
        if attempt is None:
            raise ValueError('No retained calibration attempt with that ID')
        attempt['included'] = False

    def ranked_settings(self):
        # Each attempt contributes one best-pair score for each tested current
        # setting. Repeated attempts contribute equally, including failed ones.
        scores = defaultdict(list)
        for attempt in self.attempts:
            if not attempt['included']:
                continue
            local = {}
            for result in attempt['results']:
                key = tuple(result['leds'])
                local[key] = max(local.get(key, -1.), result['score'])
            for key, score in local.items():
                scores[key].append(score)
        ranked = [dict(leds=list(key), score=sum(values)/len(values), observations=len(values))
                  for key, values in scores.items()]
        return sorted(ranked, key=lambda r: (-r['score'], -r['observations'], r['leds']))

    def summaries(self):
        return [dict(id=a['id'], included=a['included'], status=a['status'], reason=a['reason'],
                     settings=len({tuple(r['leds']) for r in a['results']}),
                     qualified_pairs=sum(bool(r['qualified']) for r in a['results']))
                for a in self.attempts]
