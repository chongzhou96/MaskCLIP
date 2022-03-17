# Copyright (c) OpenMMLab. All rights reserved.
import torch
import torch.nn as nn
import torch.nn.functional as F

from mmcv.utils import print_log
from mmseg.utils import get_root_logger
from ..builder import HEADS
from .decode_head import BaseDecodeHead

@HEADS.register_module()
class MaskClipHead(BaseDecodeHead):

    def __init__(self, text_categories, text_channels, text_embeddings_path,
                    visual_projs_path, vit=False, bg_thresh=0.,
                    num_vote=0, vote_thresh=0., topk_text=0, 
                    cls_thresh=0., attn_pooling=False, num_heads=32,
                    **kwargs):
        super(MaskClipHead, self).__init__(**kwargs)

        self.text_categories = text_categories
        self.text_channels = text_channels
        self.text_embeddings_path = text_embeddings_path
        self.visual_projs_path = visual_projs_path

        self.register_buffer('text_embeddings', torch.randn(text_categories, text_channels))
        
        self.vit = vit
        if vit:
            self.proj = nn.Conv2d(self.in_channels, text_channels, 1, bias=False)
        else:
            self.q_proj = nn.Conv2d(self.in_channels, self.in_channels, 1)
            self.k_proj = nn.Conv2d(self.in_channels, self.in_channels, 1)
            self.v_proj = nn.Conv2d(self.in_channels, self.in_channels, 1)
            self.c_proj = nn.Conv2d(self.in_channels, text_channels, 1)

        self.bg_thresh = bg_thresh
        self.num_vote = num_vote
        if not isinstance(vote_thresh, list):
            vote_thresh = [vote_thresh] * num_vote
        self.vote_thresh = vote_thresh
        self.topk_text = topk_text
        self.cls_thresh = cls_thresh
        self.attn_pooling = attn_pooling
        self.num_heads = num_heads

        self.load_text_embeddings()
        self.load_visual_projs()

    def init_weights(self):
        super(MaskClipHead, self).init_weights()
        self.load_text_embeddings()
        self.load_visual_projs()

    def load_text_embeddings(self):
        loaded = torch.load(self.text_embeddings_path, map_location='cuda')
        self.text_embeddings[:, :] = loaded[:, :]
        print_log(f'Loaded text embeddings from {self.text_embeddings_path}', logger=get_root_logger())

    def load_visual_projs(self):
        loaded = torch.load(self.visual_projs_path, map_location='cuda')
        attrs = ['proj'] if self.vit else ['q_proj', 'k_proj', 'v_proj', 'c_proj']
        for attr in attrs:
            current_attr = getattr(self, attr)
            state_dict = loaded[attr]
            for key in state_dict:
                if 'weight' in key:
                    state_dict[key] = state_dict[key][:, :, None, None]
            current_attr.load_state_dict(state_dict)
        print_log(f'Loaded proj weights from {self.visual_projs_path}', logger=get_root_logger())
    
    def forward(self, inputs):
        x = self._transform_inputs(inputs)
        q, k, v, cls_token = None, None, None, None
        if self.vit:
            if isinstance(x, list) and len(x) == 4:
                x, q, k, v = x
            if isinstance(x, list) and len(x) == 2:
                x, cls_token = x
            if v is not None:
                feat = self.proj(v)
            else:
                feat = self.proj(x)
            if cls_token is not None:
                cls_token = self.proj(cls_token[:, :, None, None])[:, :, 0, 0]
        else:
            if self.attn_pooling:
                N, C, H, W = x.shape
                x = x.view(N, C, -1).permute(2, 0, 1)  # NCHW -> (HW)NC
                x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)
                x, _ = F.multi_head_attention_forward(
                    query=x, key=x, value=x,
                    embed_dim_to_check=x.shape[-1],
                    num_heads=self.num_heads,
                    q_proj_weight=self.q_proj.weight[:, :, 0, 0],
                    k_proj_weight=self.k_proj.weight[:, :, 0, 0],
                    v_proj_weight=self.v_proj.weight[:, :, 0, 0],
                    in_proj_weight=None,
                    in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
                    bias_k=None,
                    bias_v=None,
                    add_zero_attn=False,
                    dropout_p=0,
                    out_proj_weight=self.c_proj.weight[:, :, 0, 0],
                    out_proj_bias=self.c_proj.bias,
                    use_separate_proj_weight=True,
                    training=self.training,
                    need_weights=False
                )
                feat = x[1:].permute(1, 2, 0).view(N, -1, H, W)
            else:
                q = self.q_proj(x)
                k = self.k_proj(x)
                q = torch.flatten(q, start_dim=2).transpose(-2, -1)
                k = torch.flatten(k, start_dim=2).transpose(-2, -1)
                v = self.v_proj(x)
                feat = self.c_proj(v)
        output = self.cls_seg(feat)
        if not self.training:
            output = self.refine_output(output, k , cls_token)

        return output

    def cls_seg(self, feat):
        feat = feat / feat.norm(dim=1, keepdim=True)
        output = F.conv2d(feat, self.text_embeddings[:, :, None, None])
        
        return output

    def refine_output(self, output, k, cls_token):
        output2logits = False

        if self.topk_text > 0:
            assert cls_token is not None, 'Please set `output_cls_token=True` in the backbone config.'
            cls_pred = cls_token @ self.text_embeddings.t()
            _, selected_cls = cls_pred.topk(self.topk_text, dim=1)
            N, C, H, W = output.shape
            selected_cls = selected_cls[:, :, None, None].expand(-1, -1, H, W)
            zeros = output.new_full((N, C, H, W), -100)
            zeros[selected_cls] = output[selected_cls]
            output = zeros
        elif self.cls_thresh > 0:
            N, C, H, W = output.shape
            _output = F.softmax(output*100, dim=1)
            max_cls_conf = _output.view(N, C, -1).max(dim=-1)[0]
            selected_cls = (max_cls_conf < self.cls_thresh)[:, :, None, None].expand(N, C, H, W)
            output[selected_cls] = -100

        if k is not None and self.num_vote > 0:
            if not output2logits:
                output = F.softmax(output*100, dim=1)
                output2logits = True
            N, C, H, W = output.shape
            output = output.view(N, C, -1).transpose(-2, -1)
            # softmax
            # attn = k @ k.transpose(-2, -1)
            # attn = F.softmax(attn, dim=-1)
            # L2 distance
            k = F.normalize(k, p=2)
            attn = k @ k.transpose(-2, -1)

            for i in range(self.num_vote):
                if len(self.vote_thresh):
                    # mask the unconfident
                    # masked_attn = attn.clone()
                    # selected_pos = (output.max(dim=-1, keepdim=True)[0] < self.vote_thresh[i])
                    # _selected_pos = selected_pos.expand(-1, -1, attn.shape[2])
                    # masked_attn[_selected_pos] = 0
                    # masked_attn[:, range(H*W), range(H*W)] = attn[:, range(H*W), range(H*W)]
                    
                    # KNN
                    # knn = masked_attn.topk(5, dim=-1)[1]
                    # vote_output = output.view(-1, C)[knn.view(-1, 5)]
                    # vote_output = vote_output.mean(dim=-2)
                    # vote_output = vote_output.view(N, -1, C)
                    
                    # mask the unconfident
                    # vote_output = masked_attn @ output

                    vote_output = attn @ output

                    selected_pos = (output.max(dim=-1, keepdim=True)[0] < self.vote_thresh[i])
                    _selected_pos = selected_pos.expand(-1, -1, C)
                    output[_selected_pos] = vote_output[_selected_pos]
                else:
                    output = attn @ output
            output = output.transpose(-2, -1).view(N, C, H, W)

        bg_output = None
        if self.text_categories > self.num_classes:
            bg_output, _ = torch.max(output[:, self.num_classes:], dim=1, keepdim=True)
        elif self.bg_thresh > 0:
            if not output2logits:
                output = F.softmax(output*100, dim=1)
                output2logits = True
            N, C, H, W = output.shape
            bg_output = output.new_full((N, 1, H, W), self.bg_thresh)
        if bg_output is not None:
            output = torch.cat([bg_output, output[:, :self.num_classes]], dim=1)

        return output

    # def forward_train(self, inputs, img_metas, gt_semantic_seg, train_cfg):
    #     raise RuntimeError('MaskClip is not trainable. Try MaskClip+ instead.')