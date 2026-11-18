import io

import pytest

spm = pytest.importorskip("sentencepiece")
tokenizers = pytest.importorskip("tokenizers")
from sentencepiece import sentencepiece_model_pb2

from mcp_memory_service.embeddings.sentencepiece_tokenizer import XLMRobertaSentencePieceTokenizer


@pytest.fixture
def pair(tmp_path):
    buffer = io.BytesIO()
    corpus = [
        "Synthetic reference memory database backup restore and shared context.",
        "Проверка общей памяти, резервной копии и восстановления данных.",
        "Unicode café e\u0301 中文 한국어 😀 /home/example/project C:\\Users\\Example",
    ] * 20
    spm.SentencePieceTrainer.train(
        sentence_iterator=iter(corpus), model_writer=buffer,
        vocab_size=100, hard_vocab_limit=False, model_type="unigram",
        normalization_rule_name="nmt_nfkc", num_threads=1, minloglevel=2,
    )
    proto = sentencepiece_model_pb2.ModelProto.FromString(buffer.getvalue())
    for word, score in [("XX", -2.0), ("XXXX", -4.0), ("review", -7.123456), (".", -2.987654)]:
        piece = next((item for item in proto.pieces if item.piece == word), None)
        if piece is None:
            piece = proto.pieces.add(piece=word)
        piece.score = score
    proto.trainer_spec.vocab_size = len(proto.pieces)
    path = tmp_path / "sentencepiece.bpe.model"
    path.write_bytes(proto.SerializeToString())
    compact = XLMRobertaSentencePieceTokenizer(path)
    native = compact.processor
    vocab = [("<s>", 0.0), ("<pad>", 0.0), ("</s>", 0.0), ("<unk>", 0.0)]
    vocab += [(native.id_to_piece(i), native.get_score(i)) for i in range(3, native.get_piece_size())]
    vocab.append(("<mask>", 0.0))
    reference = tokenizers.Tokenizer(tokenizers.models.Unigram(vocab, 3))
    reference.normalizer = tokenizers.normalizers.Sequence([
        tokenizers.normalizers.Precompiled(proto.normalizer_spec.precompiled_charsmap),
        tokenizers.normalizers.Replace(tokenizers.Regex(r"\s+"), " "),
        tokenizers.normalizers.Strip(),
    ])
    reference.pre_tokenizer = tokenizers.pre_tokenizers.Metaspace()
    reference.decoder = tokenizers.decoders.Metaspace()
    reference.add_special_tokens([
        tokenizers.AddedToken(word, special=True, lstrip=word == "<mask>", normalized=False)
        for word in compact.special
    ])
    reference.post_processor = tokenizers.processors.TemplateProcessing(
        single="<s> $A </s>", special_tokens=[("<s>", 0), ("</s>", 2)],
    )
    reference.enable_padding(pad_id=1, pad_token="<pad>")
    return compact, reference


@pytest.mark.parametrize("maximum", [3, 16, 128, 512])
@pytest.mark.parametrize("special", [True, False])
def test_ids_masks_overflow_and_decode_match_reference(pair, maximum, special):
    compact, reference = pair
    compact.enable_truncation(maximum)
    reference.enable_truncation(max_length=maximum)
    for text in ["", " ", "Проверка памяти", "backup restore", "café 😀 中文",
                 "a <mask> b", "<s>memory</s>", "<unk><pad>", "  shared\ncontext  ",
                 "memory database " * 100, "review.XXXXXX);", "0000000000",
                 "database backup " * 40 + "review.XXXXXX); " * 8]:
        left = compact.encode(text, add_special_tokens=special)
        right = reference.encode(text, add_special_tokens=special)
        left_parts = [left] + left.overflowing
        right_parts = [right] + right.overflowing
        assert [x.ids for x in left_parts] == [x.ids for x in right_parts]
        assert [x.attention_mask for x in left_parts] == [x.attention_mask for x in right_parts]
        assert [x.type_ids for x in left_parts] == [x.type_ids for x in right_parts]
        for skip in [True, False]:
            assert compact.decode(left.ids, skip_special_tokens=skip) == reference.decode(right.ids, skip_special_tokens=skip)


def test_batch_matches_individual_inputs_and_rejects_invalid_limits(pair):
    compact, reference = pair
    texts = ["Проверка", "backup restore", ""]
    left, right = compact.encode_batch(texts), reference.encode_batch(texts)
    assert [x.ids for x in left] == [x.ids for x in right]
    assert [x.attention_mask for x in left] == [x.attention_mask for x in right]
    for limit in [0, 1, 2, True, "512"]:
        with pytest.raises(ValueError):
            compact.enable_truncation(limit)
