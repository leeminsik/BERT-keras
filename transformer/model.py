import json
import keras
import numpy as np
import keras.backend as K
from data.vocab import TextEncoder
from transformer.config import BERTConfig
from transformer.embedding import Embedding
from keras.layers import Conv1D, Dropout, Add, Input, TimeDistributed
from transformer.layers import SelfAttention, Gelu, LayerNormalization, PositionIdGenerator, TiedEmbeddingsTransposed


class MultiHeadAttention:
    def __init__(self, n_state, n_head, attention_dropout, ignore_mask, layer_id):
        assert n_state % n_head == 0
        self.c_attn = Conv1D(3 * n_state, 1, name='layer_{}/c_attn'.format(layer_id))
        self.self_attn = SelfAttention(n_head, n_state, attention_dropout, ignore_mask,
                                       name='layer_{}/self_attention'.format(layer_id))
        self.c_attn_proj = Conv1D(n_state, 1, name='layer_{}/c_attn_proj'.format(layer_id))

    def __call__(self, x, mask):
        output = self.c_attn(x)
        output = self.self_attn(output) if mask is None else self.self_attn([output, mask])
        return self.c_attn_proj(output)


class PositionWiseFF:
    def __init__(self, n_state: int, d_hid: int, layer_id: int):
        self.c_fc = Conv1D(d_hid, 1, name='layer_{}/c_fc'.format(layer_id))
        self.activation = Gelu(name='layer_{}/gelu'.format(layer_id))
        self.c_ffn_proj = Conv1D(n_state, 1, name='layer_{}/c_ffn_proj'.format(layer_id))

    def __call__(self, x):
        output = self.activation(self.c_fc(x))
        return self.c_ffn_proj(output)


class EncoderLayer:
    def __init__(self, n_state, n_head, d_hid, residual_dropout, attention_dropout, ignore_mask, layer_id: int):
        self.attention = MultiHeadAttention(n_state, n_head, attention_dropout, ignore_mask, layer_id)
        self.drop1 = Dropout(residual_dropout, name='layer_{}/ln_1_drop'.format(layer_id))
        self.add1 = Add(name='layer_{}/ln_1_add'.format(layer_id))
        self.ln1 = LayerNormalization(name='layer_{}/ln_1'.format(layer_id))
        self.ffn = PositionWiseFF(n_state, d_hid, layer_id)
        self.drop2 = Dropout(residual_dropout, name='layer_{}/ln_2_drop'.format(layer_id))
        self.add2 = Add(name='layer_{}/ln_2_add'.format(layer_id))
        self.ln2 = LayerNormalization(name='layer_{}/ln_2'.format(layer_id))

    def __call__(self, x, mask):
        a = self.attention(x, mask)
        n = self.ln1(self.add1([x, self.drop1(a)]))
        f = self.ffn(n)
        return self.ln2(self.add2([n, self.drop2(f)]))


def create_model(embedding_dim: int = 768, embedding_dropout: float = 0.1,
                 vocab_size: int = 30000 + TextEncoder.SPECIAL_COUNT, max_len: int = 512,
                 trainable_pos_embedding: bool = True, num_heads: int = 12, num_layers: int = 12,
                 attention_dropout: float = 0.1, use_one_embedding_dropout: bool = BERTConfig.USE_ONE_DROPOUT,
                 d_hid: int = 768 * 4, residual_dropout: float = 0.1,
                 ignore_mask: bool = BERTConfig.IGNORE_MASK, debug: bool = False) -> keras.Model:
    # NOTE mask is created via create_mask
    mask = None if ignore_mask else Input(batch_shape=(None, 1, max_len, max_len), name='MaskInput', tensor=K.variable(
        np.random.randint(0, 2, (3, 1, max_len, max_len)).astype(np.float32)) if debug else None)
    tokens = Input(batch_shape=(None, max_len), name='TokenInput',
                   tensor=K.variable(np.random.randint(0, vocab_size, (3, max_len))) if debug else None)
    segment_ids = Input(batch_shape=(None, max_len), name='SegmentInput',
                        tensor=K.variable(np.random.randint(0, 2, (3, max_len))) if debug else None)
    pos_ids = PositionIdGenerator(name='PositionInput')(tokens)
    embedding_layer = Embedding(embedding_dim, embedding_dropout, vocab_size, max_len, trainable_pos_embedding,
                                use_one_embedding_dropout)
    x = embedding_layer(tokens, segment_ids, pos_ids)
    for i in range(num_layers):
        x = EncoderLayer(embedding_dim, num_heads, d_hid, residual_dropout, attention_dropout, ignore_mask, i)(x, mask)
    logits = TimeDistributed(TiedEmbeddingsTransposed(embedding_layer.token_emb, units=vocab_size, name='TiedDecoder'),
                             name='TiedDecoderDistributed')(x)
    if debug:
        print(K.eval(x).shape, K.eval(logits).shape)
    return keras.Model(inputs=[tokens, segment_ids] + ([] if ignore_mask else [mask]), outputs=[x, logits],
                       name='Transformer')


def load_openai_model(path: str = './openai_weights/', ignore_mask: bool = False,
                      use_one_embedding_dropout: bool = False, debug: bool = False) -> keras.Model:
    shapes = json.load(open(path + 'params_shapes.json'))
    offsets = np.cumsum([np.prod(shape) for shape in shapes])
    init_params = [np.load(path + 'params_{}.npy'.format(n)) for n in range(10)]
    init_params = np.split(np.concatenate(init_params, 0), offsets)[:-1]
    init_params = [param.reshape(shape) for param, shape in zip(init_params, shapes)]
    # add special token embedding to token embedding
    init_params[1] = np.concatenate(
        (init_params[1], np.random.randn(TextEncoder.SPECIAL_COUNT, 768).astype(np.float32) * 0.02), axis=0)
    init_params = [np.zeros((TextEncoder.NUM_SEGMENTS, 768)).astype(np.float32)] + init_params  # segment embedding
    init_params = init_params + [np.zeros((40478 + TextEncoder.SPECIAL_COUNT,)).astype(np.float32)]  # decoder's bias
    model = create_model(embedding_dim=768, embedding_dropout=0.1, vocab_size=40478 + TextEncoder.SPECIAL_COUNT,
                         max_len=512, ignore_mask=ignore_mask, trainable_pos_embedding=True, num_heads=12,
                         num_layers=12, use_one_embedding_dropout=use_one_embedding_dropout, d_hid=4 * 768,
                         attention_dropout=0.1, residual_dropout=0.1, debug=debug)
    if debug:
        assert len(model.weights) == len(init_params)
        for a, b in zip(model.weights, init_params):
            assert a.shape == b.shape
    model.set_weights(init_params)
    return model
