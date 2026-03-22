import unittest

from app.llm_rewriter import split_paragraph_into_rewrite_units


class SplitParagraphIntoRewriteUnitsTests(unittest.TestCase):
    def test_short_paragraph_kept_as_single_unit(self):
        text = "TextRank算法适用于关键词提取。"
        self.assertEqual(split_paragraph_into_rewrite_units(text), [text])

    def test_long_paragraph_is_split_into_sentence_groups_without_losing_text(self):
        text = (
            "随着信息技术的快速发展，学术文献数量持续增长。"
            "传统人工处理方式耗时较长，且一致性不足。"
            "为提升处理效率，研究引入TextRank算法进行关键词提取。"
            "在此基础上，系统进一步结合TF-IDF特征与朴素贝叶斯模型完成文本分类。"
            "实验结果表明，该方法在准确率与部署效率之间取得了较为平衡的表现。"
        )
        units = split_paragraph_into_rewrite_units(text, min_chars=30, max_chars=60)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(units), text)
        self.assertTrue(all(unit.strip() for unit in units))


if __name__ == "__main__":
    unittest.main()
