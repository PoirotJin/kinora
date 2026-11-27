import unittest
from agent_context import prepare,encode


class ContextTests(unittest.TestCase):
    def payload(self):
        return dict(topics=[dict(id='walking',name='走路',definition='日常走路')],duration_s=8,
            candidates=[dict(topic_id='walking',text='A person walks forward at a steady pace.')],
            history_json={'walking':dict(total_saved_prompts=1000000,
                family_counts={str(i):i for i in range(200)},examples=[dict(id=str(i),text='A person walks. '*40) for i in range(200)])},
            rejected_feedback=[{'reason':'Feedback '*1000} for _ in range(50)],
            candidate_neighbors=[{'candidate_index':0,'matches':[dict(text_id=str(i),text='A person walks backward. '*35,similarity=.9) for i in range(100)]}])

    def test_generation_counts_and_byte_budget_are_bounded(self):
        result,meta,evidence=prepare('generate','S'*10000,self.payload())
        self.assertLessEqual(meta['input_bytes'],16000)
        self.assertLessEqual(len(result['history_json']['walking']['examples']),6)
        self.assertLessEqual(len(result['rejected_feedback']),2)
        self.assertEqual(result['count_per_topic'],1);self.assertFalse(evidence)

    def test_review_uses_only_selected_evidence_and_preserves_candidate(self):
        source=self.payload();result,meta,evidence=prepare('review','S'*10000,source)
        self.assertLessEqual(meta['input_bytes'],12000);self.assertNotIn('history_json',result)
        self.assertEqual(result['candidates'],source['candidates'])
        self.assertLessEqual(len(evidence),5)
        self.assertEqual(list(evidence),[m['text_id'] for m in result['candidate_neighbors'][0]['matches']])
        self.assertGreater(meta['dropped_items'],0)

    def test_oversized_core_fails_and_unicode_bytes_count(self):
        source=self.payload();source['topics'][0]['definition']='日常'*10000
        with self.assertRaises(ValueError):prepare('review','rules',source)
        source=self.payload();result,meta,_=prepare('review','rules',source)
        self.assertEqual(meta['input_bytes'],len(('rules'+encode(result)).encode('utf-8')))

    def test_history_growth_does_not_increase_request(self):
        a=self.payload();b=self.payload();b['history_json']['walking']['examples']*=100
        self.assertEqual(prepare('generate','rules',a)[0],prepare('generate','rules',b)[0])


if __name__=='__main__':unittest.main()
