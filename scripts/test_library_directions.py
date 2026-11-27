"""Manually labelled bilingual retrieval cases, independent of vector similarity."""
import unittest
from library_directions import describe, prepare, constrain


class DirectionTests(unittest.TestCase):
    def matches(self, query, text):
        result = constrain(query, prepare([('prompt', text, 4)]))
        self.assertIsNotNone(result, query)
        return bool(result[0])

    def test_bilingual_travel_and_conflicts(self):
        cases = [
            ('A person walks to their right.', True),
            ('A person moves sideways to the right.', True),
            ('A person walks diagonally forward and to their right.', True),
            ('A person walks to the left.', False),
            ('A person walks right, then steps left.', False),
            ('A person walks diagonally to the right, then moves directly to the left.', False),
            ('A person walks left, then right.', False),
            ('A person walks forward without any lateral movement.', False),
            ('A person runs right with side-to-side shuffles.', False),
            ('A person runs right. They maintain continuous side-to-side motion.', False),
            ('A person walks right with arms swinging side-to-side.', True),
            ('A person walks forward facing right.', False),
            ('A person moves in a continuous motion to stand facing right.', False),
            ('A person raises the right hand while standing still.', False),
            ('A person moves the right arm forward.', False),
            ('The arms move to the right.', False),
            ('A person moves their arm to the right.', False),
            ('A person keeps balance as the right arm moves left.', False),
            ('A person turns right in place.', False),
            ('A person walks right while turning left.', True),
            ('A person walks left while turning right.', False),
            ('A person walks right without moving left.', True),
            ('A person does not move right; they walk left.', False),
            ('一个人向右侧走。', True),
            ('一个人向右横向移动，然后向左走。', False),
            ('一个人向左走，抬起右手。', False),
        ]
        for query in ('向右走', '向右横向移动', '往右侧迈步', 'move right', 'walk to the right', 'rightward walking'):
            for text, expected in cases:
                with self.subTest(query=query, text=text):
                    self.assertEqual(self.matches(query, text), expected)

    def test_mirrored_left(self):
        for query in ('向左移动', '往左走', 'walk left', 'move to the left'):
            self.assertTrue(self.matches(query, 'A person shuffles sideways to the left.'))
            self.assertFalse(self.matches(query, 'A person moves right with their left arm raised.'))
            self.assertFalse(self.matches(query, 'A person walks left then right.'))

    def test_turning_is_distinct_from_travel_and_head_motion(self):
        for query in ('向右转身', '右转', 'turn right', 'right turn'):
            self.assertTrue(self.matches(query, 'A person walks left then turns to the right.'))
            self.assertFalse(self.matches(query, 'A person walks right then turns left.'))
            self.assertFalse(self.matches(query, 'A person turns right then turns left.'))
            self.assertFalse(self.matches(query, 'A person turns their head right.'))
            self.assertFalse(self.matches(query, 'A person walks right.'))

    def test_negation_and_sequences(self):
        for query in ('不要向左，向右走', 'move right, not left'):
            self.assertTrue(self.matches(query, 'A person walks right without moving left.'))
            self.assertFalse(self.matches(query, 'A person walks left, not right.'))
        for query in ('先向右走然后向左走', 'walk right then left'):
            self.assertTrue(self.matches(query, 'A person moves right then walks left.'))
            self.assertFalse(self.matches(query, 'A person moves left then walks right.'))
            self.assertFalse(self.matches(query, 'A person walks right.'))
        self.assertTrue(self.matches('先向右再向左', 'A person walks right then left.'))
        for query in ('不要向右走', 'do not move right'):
            self.assertFalse(self.matches(query, 'A person walks right.'))
            self.assertTrue(self.matches(query, 'A person walks left.'))

    def test_limb_and_non_direction_queries_keep_existing_search(self):
        for query in ('抬右手', '右手向左挥动', 'raise the right hand', 'lift left leg', 'walking', '转弯', 'm0000c0fi'):
            self.assertIsNone(constrain(query, prepare([])), query)


if __name__ == '__main__':
    unittest.main()
