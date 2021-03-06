import torch
import torch.nn as nn
import torch.nn.functional as F


class GlobalStyleTokens(nn.Module):
    """Global Style Token Module for factorizing prosody in speech.

    See https://arxiv.org/pdf/1803.09017"""

    # def __init__(self, num_mel, num_heads, num_style_tokens,
    #              text_encoding_dim, prosody_encoding_dim):
    def __init__(self, num_mel, num_style_tokens, token_dim,
                 prosody_encoding_dim, scoring_function_name,
                 use_separate_keys):
        super().__init__()
        scoring_function = getattr(torch, scoring_function_name)
        self.encoder = ReferenceEncoder(num_mel, prosody_encoding_dim)
        self.style_token_layer = StyleTokenLayer(num_style_tokens,
                                                 token_dim,
                                                 prosody_encoding_dim,
                                                 scoring_function,
                                                 use_separate_keys)

    def forward(self, inputs):
        enc_out = self.encoder(inputs)
        style_embed, token_scores = self.style_token_layer(enc_out)

        return style_embed, token_scores

    def inference(self, token_scores):
        return self.style_token_layer.inference(token_scores)


class ReferenceEncoder(nn.Module):
    """NN module creating a fixed size prosody embedding from a spectrogram.

    inputs: mel spectrograms [batch_size, num_spec_frames, num_mel]
    outputs: [batch_size, prosody_encoding_dim]
    """

    def __init__(self, num_mel, prosody_encoding_dim):

        super().__init__()
        self.num_mel = num_mel
        filters = [1] + [32, 32, 64, 64, 128, 128]
        num_layers = len(filters) - 1
        convs = [nn.Conv2d(in_channels=filters[i],
                           out_channels=filters[i + 1],
                           kernel_size=(3, 3),
                           stride=(2, 2),
                           padding=(1, 1)) for i in range(num_layers)]
        self.convs = nn.ModuleList(convs)
        self.bns = nn.ModuleList(
            [nn.BatchNorm2d(num_features=filter_size) for filter_size in
             filters[1:]])

        post_conv_height = self.calculate_post_conv_height(num_mel, 3,
                                                           2, 1, num_layers)
        # self.recurrence = nn.LSTM(input_size=filters[-1] * post_conv_height,
        #                           hidden_size=prosody_encoding_dim,
        #                           batch_first=True)
        self.recurrence = nn.GRU(input_size=filters[-1] * post_conv_height,
                                 hidden_size=prosody_encoding_dim,
                                 batch_first=True)

    def forward(self, inputs):
        batch_size = inputs.size(0)
        x = inputs.view(batch_size, 1, -1, self.num_mel)
        # x: 4D tensor [batch_size, num_channels==1, num_frames, num_mel]
        for conv, bn in zip(self.convs, self.bns):
            x = conv(x)
            x = F.relu(x)
            x = bn(x)

        x = x.transpose(1, 2)
        # x: 4D tensor [batch_size, post_conv_width,
        #               num_channels==128, post_conv_height]
        post_conv_width = x.size(1)
        x = x.contiguous().view(batch_size, post_conv_width, -1)
        # x: 3D tensor [batch_size, post_conv_width,
        #               num_channels*post_conv_height]

        self.recurrence.flatten_parameters()
        # memory, (out, _) = self.recurrence(x)
        memory, out = self.recurrence(x)
        # out: 3D tensor [seq_len==1, batch_size, encoding_size=128]

        return out.squeeze(0)

    def calculate_post_conv_height(self, height, kernel_size,
                                   stride, pad, n_convs):
        """Height of spec after n convolutions with fixed kernel/stride/pad."""
        for i in range(n_convs):
            height = (height - kernel_size + 2 * pad) // stride + 1
        return height


class StyleTokenLayer(nn.Module):
    """NN Module attending to style tokens based on prosody encodings."""

    # def __init__(self, num_heads, num_style_tokens,
    #              text_encoding_dim, prosody_encoding_dim):
    def __init__(self, num_style_tokens,
                 token_dim, prosody_encoding_dim, scoring_function,
                 use_separate_keys=True):
        super().__init__()
        # self.token_dim = text_encoding_dim // num_heads
        self.token_dim = token_dim
        self.style_tokens = nn.Parameter(
            torch.FloatTensor(num_style_tokens, self.token_dim))
        nn.init.normal_(self.style_tokens, mean=0, std=0.3)
        if use_separate_keys:
            self.keys = nn.Parameter(
                torch.FloatTensor(num_style_tokens, self.token_dim))
            nn.init.normal_(self.keys, mean=0, std=0.3)
        else:
            self.keys = None
        self.attention = SparseTokenAttention(prosody_encoding_dim, token_dim,
                                              scoring_function)
        # self.attention = MultiTokenAttention(prosody_encoding_dim,
        #                                      self.token_dim,
        #                                      text_encoding_dim,
        #                                      num_heads)

    def forward(self, inputs):
        batch_size = inputs.size(0)
        prosody_encoding = inputs.unsqueeze(1)
        # prosody_encoding: 3D tensor [batch_size, 1, encoding_size==128]
        tokens = self.shape_for_attention(self.style_tokens, batch_size)
        keys = self.shape_for_attention(self.keys, batch_size)
        # tokens: 3D tensor [batch_size, num tokens, token embedding size]
        style_embed, token_scores = self.attention(prosody_encoding,
                                                   tokens, keys)
        return style_embed, token_scores

    def inference(self, token_scores):
        batch_size = token_scores.size(0)
        tokens = self.shape_for_attention(self.style_tokens, batch_size)
        token_scores.unsqueeze(1)
        return self.attention.inference(tokens, token_scores)

    def shape_for_attention(self, tokens, batch_size):
        """Shapes tokens and keys."""
        if tokens is None:
            return None
        else:
            return tokens.unsqueeze(0).expand(batch_size, -1, -1)


# class MultiTokenAttention(nn.Module):
#     """Attention on a set of style tokens with multiple attention heads.
#
#     Multi head attention: https://arxiv.org/abs/1706.03762"""
#
#     def __init__(self, prosody_encoding_dim, token_dim,
#                  text_encoding_dim, num_heads):
#         super().__init__()
#         self.num_heads = num_heads
#         self.prosody_encoding_dim = prosody_encoding_dim
#         self.token_dim = token_dim
#         self.text_encoding_dim = text_encoding_dim
#
#         self.W_prosody_encoding = nn.Linear(prosody_encoding_dim,
#                                             text_encoding_dim,
#                                             bias=False)
#         self.W_token = nn.Linear(token_dim, text_encoding_dim, bias=False)
#         self.W_out = nn.Linear(text_encoding_dim, text_encoding_dim,
#         bias=False)
#
#     def forward(self, prosody_encoding, tokens):
#         prosody_encoding = self.W_prosody_encoding(prosody_encoding)
#         # prosody_encoding: 3D Tensor [batch_size, 1,
#         #                              text encoding dim]
#         tokens = self.W_token(tokens)
#         # tokens: 3D Tensor [batch_size, num tokens, text encoding dim]
#
#         prosody_encoding = torch.stack(
#             torch.split(prosody_encoding, self.token_dim, dim=2), dim=0)
#         # prosody_encoding: 4D tensor [num_heads, batch_size, 1,
#         #                               token dim]
#         tokens = torch.stack(
#             torch.split(tokens, self.token_dim, dim=2), dim=0)
#         # tokens: 4D tensor [num heads, batch size, num tokens, token dim]
#
#         # score = softmax(QK^T / (d_k ** 0.5))
#         scores = torch.matmul(prosody_encoding, tokens.transpose(2, 3))
#         # scores: 4D tensor [num heads, batch size, 1, num tokens]
#         scores = scores / (self.token_dim ** 0.5)
#         scores = F.softmax(scores, dim=3)
#
#         # out = score * V
#         out = torch.matmul(scores, tokens)
#         # out: 4D tensor [num heads, batch size, 1, token dim]
#         out = torch.cat(torch.split(out, 1, dim=0), dim=3).squeeze(0)
#         # out: 3D tensor [batch_size, 1, text encoding dim]
#         out = self.W_out(out)
#
#         return out


class SparseTokenAttention(nn.Module):
    """Attention on a set of sparse style tokens."""

    def __init__(self, prosody_encoding_dim, token_dim, scoring_function):
        super().__init__()
        self.prosody_encoding_dim = prosody_encoding_dim
        self.token_dim = token_dim
        self.scoring_function = scoring_function

        self.W_prosody_encoding = nn.Linear(prosody_encoding_dim,
                                            token_dim)
        self.W_out = nn.Linear(token_dim, token_dim)

    def forward(self, prosody_encoding, tokens, keys=None):
        scores = self.get_scores(prosody_encoding, tokens, keys)
        return self.inference(tokens, scores), scores

    def inference(self, tokens, scores):
        # out = score * V
        out = torch.matmul(scores, tokens)
        # out: 3D tensor [batch size, 1, token dim]
        out = self.W_out(out)

        return out

    def get_scores(self, prosody_encoding, tokens, keys=None):
        prosody_encoding = self.W_prosody_encoding(prosody_encoding)
        # prosody_encoding: 3D Tensor [batch_size, 1, token_dim]
        # tokens: 3D Tensor [batch_size, num tokens, token_dim]
        # keys 3D Tensor like tokens or None

        keys = keys if keys is not None else tokens
        scores = torch.matmul(prosody_encoding, keys.transpose(1, 2))
        # scores: 3D tensor [batch size, 1, num tokens]
        # scores = scores / (self.token_dim ** 0.5)
        scores = self.scoring_function(scores)
        return scores
