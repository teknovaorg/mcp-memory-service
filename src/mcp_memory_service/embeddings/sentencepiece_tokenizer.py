"""Compact SentencePiece adapter for the XLM-R token ID convention."""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re


@dataclass
class Encoding:
    ids: list[int]
    overflowing: list["Encoding"] = field(default_factory=list)
    padding: int = 0

    @property
    def attention_mask(self):
        return [1] * (len(self.ids) - self.padding) + [0] * self.padding

    @property
    def type_ids(self):
        return [0] * len(self.ids)


class XLMRobertaSentencePieceTokenizer:
    def __init__(self, model_path):
        import sentencepiece

        self.processor = sentencepiece.SentencePieceProcessor(
            model_file=str(Path(model_path))
        )
        if (self.processor.unk_id(), self.processor.bos_id(), self.processor.eos_id()) != (0, 1, 2):
            raise ValueError("SentencePiece model does not use XLM-R reserved IDs")
        self.special = {
            "<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3,
            "<mask>": self.processor.get_piece_size() + 1,
        }
        self.special_by_id = {value: key for key, value in self.special.items()}
        self.pattern = re.compile("(" + "|".join(re.escape(x) for x in self.special) + ")")
        self.max_length = None
        self.max_piece_length = max(len(self.processor.id_to_piece(i)) for i in range(self.processor.get_piece_size()))
        self.unknown_score = min(self.processor.get_score(i) for i in range(self.processor.get_piece_size())) - 10.0
        self._lookup = lru_cache(maxsize=8192)(self._piece)
        self._word = lru_cache(maxsize=1024)(self._encode_word)

    def _piece(self, value):
        if value in self.special:
            return self.special[value], 0.0
        ident = self.processor.piece_to_id(value)
        return (ident + 1, self.processor.get_score(ident)) if ident else None

    def _encode_word(self, word):
        text = "▁" + word
        scores = [float("-inf")] * (len(text) + 1)
        previous = [None] * (len(text) + 1)
        scores[0] = 0.0
        for start in range(len(text)):
            single = False
            for end in range(start + 1, min(len(text), start + self.max_piece_length) + 1):
                piece = self._lookup(text[start:end])
                if piece is None:
                    continue
                ident, score = piece
                single = single or end == start + 1
                candidate = scores[start] + score
                if candidate > scores[end]:
                    scores[end], previous[end] = candidate, (start, ident)
            if not single:
                candidate = scores[start] + self.unknown_score
                if candidate > scores[start + 1]:
                    scores[start + 1], previous[start + 1] = candidate, (start, 3)
        result = []
        cursor = len(text)
        while cursor:
            cursor, ident = previous[cursor]
            result.append(ident)
        result.reverse()
        return tuple(ident for i, ident in enumerate(result) if ident != 3 or i == 0 or result[i - 1] != 3)

    def enable_truncation(self, max_length):
        if type(max_length) is not int or max_length < 3:
            raise ValueError("XLM-R truncation requires at least three tokens")
        self.max_length = max_length

    def encode(self, text, add_special_tokens=True):
        if not isinstance(text, str):
            raise TypeError("Tokenizer input must be a string")
        ids = []
        for part in self.pattern.split(text):
            if not part:
                continue
            if part in self.special:
                ids.append(self.special[part])
            else:
                for word in self.processor.normalize(part).split("▁"):
                    if word:
                        ids.extend(self._word(word) if len(word) <= 64 else self._encode_word(word))
        budget = self.max_length
        if budget is not None and add_special_tokens:
            budget -= 2
        parts = [ids] if budget is None or not ids else [ids[i:i + budget] for i in range(0, len(ids), budget)]
        encodings = [Encoding([0] + part + [2] if add_special_tokens else part) for part in parts]
        self._pad(encodings)
        encodings[0].overflowing = encodings[1:]
        return encodings[0]

    def encode_batch(self, texts):
        results = [self.encode(text) for text in texts]
        self._pad([part for result in results for part in [result] + result.overflowing])
        return results

    @staticmethod
    def _pad(encodings):
        maximum = max((len(item.ids) for item in encodings), default=0)
        for item in encodings:
            missing = maximum - len(item.ids)
            item.ids.extend([1] * missing)
            item.padding += missing

    def decode(self, ids, skip_special_tokens=True):
        pieces = []
        for ident in ids:
            if ident in self.special_by_id:
                if not skip_special_tokens:
                    pieces.append(self.special_by_id[ident])
            else:
                pieces.append(self.processor.id_to_piece(ident - 1))
        result = "".join(pieces).replace("▁", " ")
        return result[1:] if result.startswith(" ") else result
