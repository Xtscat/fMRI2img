import collections
import math
import random

import numpy as np
import torch
import torch.nn as nn
from torchvision.transforms import v2


class Clipper(torch.nn.Module):
    def __init__(
        self,
        clip_variant,
        clearclip = False,
        layer_start = 0,
        clamp_embs = False,
        norm_embs = False,
        hidden_state = False,
        device = torch.device('cpu')
    ):
        super().__init__()
        assert clip_variant in ("ViT-L/14", "ViT-B/32", "GIT-ViT"), \
            "clip_variant must be one of ViT-L/14, ViT-B/32, GIT-ViT"
        print(clip_variant, device)
        if clip_variant == "ViT-L/14":
            """ to train fmri_img fmri_text """
            from transformers import (CLIPTextModelWithProjection,
                                      CLIPTokenizer,
                                      CLIPVisionModelWithProjection)
            image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                "openai/clip-vit-large-patch14",
                cache_dir = "/media/SSD_1_2T/xt/weights/",
                clearclip = clearclip,
                layer_start = layer_start
            ).eval()
            image_encoder = image_encoder.to(device)
            for param in image_encoder.parameters():
                param.requires_grad = False
            self.image_encoder = image_encoder

            text_encoder = CLIPTextModelWithProjection.from_pretrained(
                "openai/clip-vit-large-patch14", cache_dir = "/media/SSD_1_2T/xt/weights/"
            ).eval()
            text_encoder = text_encoder.to(device)
            for param in text_encoder.parameters():
                param.requires_grad = False
            self.text_encoder = text_encoder
            self.tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")

        elif clip_variant == "ViT-B/32":
            """ to train fmri_img_sketch """
            from transformers import CLIPVisionModelWithProjection
            image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                "openai/clip-vit-base-patch32",
                cache_dir = "/media/SSD_1_2T/xt/weights/",
                clearclip = False,
                layer_start = 0
            ).eval()
            image_encoder = image_encoder.to(device)
            for param in image_encoder.parameters():
                param.requires_grad = False
            self.image_encoder = image_encoder

            for i in range(self.image_encoder.config.num_hidden_layers):
                self.image_encoder.vision_model.encoder.layers[i].register_forward_hook(self.make_hook(i))

        elif clip_variant == "GIT-ViT":
            """ to train fmri_img_GIT """
            from modeling_git import AutoProcessor, GitForCausalLMClipEmb

            # preprocess = AutoProcessor.from_pretrained("microsoft/git-base-coco", cache_dir = "/media/SSD_1_2T/xt/weights/")
            model = GitForCausalLMClipEmb.from_pretrained(
                "microsoft/git-base-coco", cache_dir = "/media/SSD_1_2T/xt/weights/"
            )
            image_encoder = model.git.image_encoder.to(device)
            for param in image_encoder.parameters():
                param.requires_grad = False
            self.image_encoder = image_encoder
            # self.git_preprocess = preprocess

        if clip_variant == "RN50x64":
            self.clip_size = (448, 448)
        else:
            self.clip_size = (224, 224)

        preprocess = v2.Compose(
            [
                v2.Resize(size = self.clip_size[0], interpolation = v2.InterpolationMode.BICUBIC, antialias = None),
                v2.ToDtype(torch.float32, scale = True),
                v2.CenterCrop(size = self.clip_size),
                v2.Normalize(mean = (0.48145466, 0.4578275, 0.40821073), std = (0.26862954, 0.26130258, 0.27577711))
            ]
        )
        self.preprocess = preprocess
        self.hidden_state = hidden_state
        self.mean = np.array([0.48145466, 0.4578275, 0.40821073])
        self.std = np.array([0.26862954, 0.26130258, 0.27577711])
        self.normalize = v2.Normalize(self.mean, self.std)
        self.denormalize = v2.Normalize((-self.mean / self.std).tolist(), (1.0 / self.std).tolist())
        self.clamp_embs = clamp_embs
        self.norm_embs = norm_embs
        self.device = device

        def versatile_normalize_embeddings(encoder_output):
            embeds = encoder_output.last_hidden_state
            embeds = image_encoder.vision_model.post_layernorm(embeds)
            embeds = image_encoder.visual_projection(embeds)
            return embeds

        self.versatile_normalize_embeddings = versatile_normalize_embeddings

    def resize_image(self, image):
        # note: antialias should be False if planning to use Pinkney's Image Variation SD model
        return v2.Resize(self.clip_size, antialias = None)(image.to(self.device))

    def make_hook(self, name):
        def hook(module, input, output):
            if isinstance(output, tuple):
                for i, out in enumerate(output):
                    self.featuremaps[name] = out
            else:
                self.featuremaps[name] = output

        return hook

    def embed_image_with_hook(self, image):
        """Expects images in -1 to 1 range"""
        self.featuremaps = collections.OrderedDict()
        clip_emb = self.preprocess((image).to(self.device))
        clip_emb = self.image_encoder(clip_emb)
        clip_emb = self.versatile_normalize_embeddings(clip_emb)
        featuremaps = [self.featuremaps[k] for k in range(self.image_encoder.config.num_hidden_layers)]

        return clip_emb, featuremaps

    def embed_image_for_GIT(self, image):
        """Expects images in -1 to 1 range"""
        clip_emb = self.preprocess((image).to(self.device))
        clip_emb = self.image_encoder(clip_emb).last_hidden_state
        return clip_emb

    def embed_image(self, image):
        """Expects images in -1 to 1 range"""
        if self.hidden_state:
            clip_emb = self.preprocess((image).to(self.device))
            clip_emb = self.image_encoder(clip_emb)
            clip_emb = self.versatile_normalize_embeddings(clip_emb)
        else:
            clip_emb = self.preprocess(image.to(self.device))
            clip_emb = self.clip.encode_image(clip_emb)
        # input is now in CLIP space, but mind-reader preprint further processes embeddings:
        if self.clamp_embs:
            clip_emb = torch.clamp(clip_emb, -1.5, 1.5)
        if self.norm_embs:
            if self.hidden_state:
                # normalize all tokens by cls token's norm
                clip_emb = clip_emb / torch.norm(clip_emb[:, 0], dim = -1).reshape(-1, 1, 1)
            else:
                clip_emb = nn.functional.normalize(clip_emb, dim = -1)
        return clip_emb

    def embed_text(self, prompt):
        def normalize_embeddings(encoder_output):
            embeds = self.text_encoder.text_projection(encoder_output.last_hidden_state)
            embeds_pooled = encoder_output.text_embeds
            embeds = embeds / torch.norm(embeds_pooled.unsqueeze(1), dim = -1, keepdim = True)
            return embeds

        text_inputs = self.tokenizer(
            prompt,
            padding = "max_length",
            max_length = self.tokenizer.model_max_length,
            truncation = True,
            return_tensors = "pt",
        )
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(prompt, padding = "max_length", return_tensors = "pt").input_ids
        with torch.no_grad():
            prompt_embeds = self.text_encoder(text_input_ids.to(self.device), )
        prompt_embeds = normalize_embeddings(prompt_embeds)
        return prompt_embeds


class Adapter_Layer(nn.Module):
    def __init__(
        self,
        in_channels,
        bottleneck = 32,
        out_channels = None,
        dropout = 0.0,
        init_option = "lora",
        adapter_scalar = "1.0",
        adapter_layernorm_option = None
    ):
        super().__init__()
        self.in_channels = in_channels
        self.down_size = bottleneck
        self.out_channels = out_channels if out_channels is not None else in_channels
        # self.non_linearity = args.non_linearity  # use ReLU by default

        #_before
        self.adapter_layernorm_option = adapter_layernorm_option

        self.adapter_layer_norm = None
        if adapter_layernorm_option == "in" or adapter_layernorm_option == "out":
            self.adapter_layer_norm = nn.LayerNorm(self.n_embd)

        if adapter_scalar == "learnable_scalar":
            self.scale = nn.Parameter(torch.ones(1))
        else:
            self.scale = float(adapter_scalar)

        self.down_proj = nn.Linear(self.in_channels, self.down_size)
        self.non_linear_func = nn.ReLU()
        self.up_proj = nn.Linear(self.down_size, self.out_channels)

        self.dropout = dropout

        if init_option == "lora":
            with torch.no_grad():
                nn.init.kaiming_uniform_(self.down_proj.weight, a = math.sqrt(5))
                nn.init.zeros_(self.up_proj.weight)
                nn.init.zeros_(self.down_proj.bias)
                nn.init.zeros_(self.up_proj.bias)

    def forward(self, x, add_residual = True, residual = None):
        residual = x if residual is None else residual
        if self.adapter_layernorm_option == 'in':
            x = self.adapter_layer_norm(x)

        down = self.down_proj(x)
        down = self.non_linear_func(down)
        down = nn.functional.dropout(down, p = self.dropout, training = self.training)
        up = self.up_proj(down)

        up = up * self.scale

        if self.adapter_layernorm_option == 'out':
            up = self.adapter_layer_norm(up)

        if add_residual:
            output = up + residual
        else:
            output = up

        return output


class ResMLP(nn.Module):
    def __init__(self, h, n_blocks, dropout = 0.15):
        super().__init__()
        self.n_blocks = n_blocks
        self.mlp = nn.ModuleList(
            [nn.Sequential(nn.Linear(h, h), nn.LayerNorm(h), nn.GELU(), nn.Dropout(dropout)) for _ in range(n_blocks)]
        )

    def forward(self, x):
        residual = x
        for res_block in range(self.n_blocks):
            x = self.mlp[res_block](x)
            x += residual
            residual = x
        return x


class MindSingle_image(nn.Module):
    def __init__(self, in_dim = 15724, out_dim_image = 768, h = 4096, n_blocks = 4, subj_list = None, adapting = False):

        super().__init__()

        self.subj_list = subj_list
        self.embedder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    Adapter_Layer(in_dim, 128), nn.Linear(in_dim, h), nn.LayerNorm(h), nn.GELU(), nn.Dropout(0.5),
                ) for subj in subj_list
            }
        )

        self.translator = ResMLP(h, n_blocks)
        self.head_image = nn.Linear(h, out_dim_image)

    # @torchsnooper.snoop()
    def forward(self, x):
        x = self.embedder[str(self.subj_list[0])](x)
        x = self.translator(x)

        x_image = self.head_image(x)

        return x_image


class MindBridge_image(MindSingle_image):
    def __init__(self, in_dim = 15724, out_dim_image = 768, h = 4096, n_blocks = 4, subj_list = None, adapting = False):

        assert len(subj_list) >= 2, "MindBridge requires at least 2 subjects"

        super().__init__(
            in_dim = in_dim, out_dim_image = out_dim_image, h = h, n_blocks = n_blocks, subj_list = subj_list
        )

        self.builder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    nn.Linear(h, in_dim), nn.LayerNorm(in_dim), nn.GELU(), Adapter_Layer(in_dim, 128),
                ) for subj in subj_list
            }
        )

        self.adapting = adapting
        self.cyc_loss = nn.MSELoss()

    # @torchsnooper.snoop()
    def forward(self, x):
        if len(x) == 2 and type(x) is tuple:
            subj_list = x[1].tolist()  # (s,)
            x = x[0]  # (b,n)
        else:
            subj_list = self.subj_list

        x = x.squeeze()
        x_subj = torch.chunk(x, len(subj_list))
        x = []
        x_rec = []
        if self.adapting:  # choose subj_a (source subject) and subj_b (target subject)
            subj_a, subj_b = subj_list[0], subj_list[-1]
        else:  # random sample 2 subjects
            subj_a, subj_b = random.sample(subj_list, 2)
        for i, subj_i in enumerate(subj_list):  # subj is 1-based
            x_i = self.embedder[str(subj_i)](x_subj[i])  # subj_i seman embedding
            if subj_i == subj_a: x_a = x_i  # subj_a seman embedding are choosen
            x.append(x_i)
            x_i_rec = self.builder[str(subj_i)](x_i)  # subj_i recon brain signals
            x_rec.append(x_i_rec)

        x = torch.concat(x, dim = 0)
        x_rec = torch.concat(x_rec, dim = 0)
        # del x_i, x_subj, x_i_rec

        # forward cycling
        x_b = self.builder[str(subj_b)](x_a)  # subj_b recon brain signal using subj_a seman embedding
        x_b = self.embedder[str(subj_b)](x_b)  # subj_b seman embedding (pseudo)
        loss_cyc = self.cyc_loss(x_a, x_b)

        x = self.translator(x)

        x_image = self.head_image(x)

        return x_image, x_rec, loss_cyc


class MindSingle_text(nn.Module):
    def __init__(self, in_dim = 15724, out_dim_text = None, h = 4096, n_blocks = 4, subj_list = None, adapting = False):

        super().__init__()

        self.subj_list = subj_list
        self.embedder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    Adapter_Layer(in_dim, 128), nn.Linear(in_dim, h), nn.LayerNorm(h), nn.GELU(), nn.Dropout(0.5),
                ) for subj in subj_list
            }
        )

        self.translator = ResMLP(h, n_blocks)
        self.head_text = nn.Linear(h, out_dim_text)

    # @torchsnooper.snoop()
    def forward(self, x):
        x = self.embedder[str(self.subj_list[0])](x)
        x = self.translator(x)

        x_text = self.head_text(x)

        return x_text


class MindBridge_text(MindSingle_text):
    def __init__(self, in_dim = 15724, out_dim_text = None, h = 4096, n_blocks = 4, subj_list = None, adapting = False):

        assert len(subj_list) >= 2, "MindBridge requires at least 2 subjects"

        super().__init__(
            in_dim = in_dim, out_dim_text = out_dim_text, h = h, n_blocks = n_blocks, subj_list = subj_list
        )

        self.builder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    nn.Linear(h, in_dim), nn.LayerNorm(in_dim), nn.GELU(), Adapter_Layer(in_dim, 128),
                ) for subj in subj_list
            }
        )

        self.adapting = adapting
        self.cyc_loss = nn.MSELoss()

    # @torchsnooper.snoop()
    def forward(self, x):
        if len(x) == 2 and type(x) is tuple:
            subj_list = x[1].tolist()  # (s,)
            x = x[0]  # (b,n)
        else:
            subj_list = self.subj_list

        x = x.squeeze()
        x_subj = torch.chunk(x, len(subj_list))
        x = []
        x_rec = []
        if self.adapting:  # choose subj_a (source subject) and subj_b (target subject)
            subj_a, subj_b = subj_list[0], subj_list[-1]
        else:  # random sample 2 subjects
            subj_a, subj_b = random.sample(subj_list, 2)
        for i, subj_i in enumerate(subj_list):  # subj is 1-based
            x_i = self.embedder[str(subj_i)](x_subj[i])  # subj_i seman embedding
            if subj_i == subj_a: x_a = x_i  # subj_a seman embedding are choosen
            x.append(x_i)
            x_i_rec = self.builder[str(subj_i)](x_i)  # subj_i recon brain signals
            x_rec.append(x_i_rec)

        x = torch.concat(x, dim = 0)
        x_rec = torch.concat(x_rec, dim = 0)
        # del x_i, x_subj, x_i_rec

        # forward cycling
        x_b = self.builder[str(subj_b)](x_a)  # subj_b recon brain signal using subj_a seman embedding
        x_b = self.embedder[str(subj_b)](x_b)  # subj_b seman embedding (pseudo)
        loss_cyc = self.cyc_loss(x_a, x_b)

        x = self.translator(x)

        x_text = self.head_text(x)

        return x_text, x_rec, loss_cyc


class MindSingle_image_GIT(nn.Module):
    def __init__(self, in_dim = 15724, out_dim_image_feature_map = 768, h = 4096, n_blocks = 4, subj_list = None, ):

        super().__init__()

        self.subj_list = subj_list
        self.embedder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    Adapter_Layer(in_dim, 128), nn.Linear(in_dim, h), nn.LayerNorm(h), nn.GELU(), nn.Dropout(0.5),
                ) for subj in subj_list
            }
        )

        self.translator = ResMLP(h, n_blocks)
        self.head_image = nn.Linear(h, out_dim_image_feature_map)

    # @torchsnooper.snoop()
    def forward(self, x):
        x = self.embedder[str(self.subj_list[0])](x)
        x = self.translator(x)

        x_image = self.head_image(x)

        return x_image


class MindBridge_image_GIT(MindSingle_image_GIT):
    def __init__(
        self,
        in_dim = 15724,
        out_dim_image_feature_map = 768,
        h = 4096,
        n_blocks = 4,
        subj_list = None,
        adapting = False
    ):

        assert len(subj_list) >= 2, "MindBridge requires at least 2 subjects"

        super().__init__(
            in_dim = in_dim,
            out_dim_image = out_dim_image_feature_map,
            h = h,
            n_blocks = n_blocks,
            subj_list = subj_list
        )

        self.builder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    nn.Linear(h, in_dim), nn.LayerNorm(in_dim), nn.GELU(), Adapter_Layer(in_dim, 128),
                ) for subj in subj_list
            }
        )

        self.adapting = adapting
        self.cyc_loss = nn.MSELoss()

    # @torchsnooper.snoop()
    def forward(self, x):
        if len(x) == 2 and type(x) is tuple:
            subj_list = x[1].tolist()  # (s,)
            x = x[0]  # (b,n)
        else:
            subj_list = self.subj_list

        x = x.squeeze()
        x_subj = torch.chunk(x, len(subj_list))
        x = []
        x_rec = []
        if self.adapting:  # choose subj_a (source subject) and subj_b (target subject)
            subj_a, subj_b = subj_list[0], subj_list[-1]
        else:  # random sample 2 subjects
            subj_a, subj_b = random.sample(subj_list, 2)
        for i, subj_i in enumerate(subj_list):  # subj is 1-based
            x_i = self.embedder[str(subj_i)](x_subj[i])  # subj_i seman embedding
            if subj_i == subj_a: x_a = x_i  # subj_a seman embedding are choosen
            x.append(x_i)
            x_i_rec = self.builder[str(subj_i)](x_i)  # subj_i recon brain signals
            x_rec.append(x_i_rec)

        x = torch.concat(x, dim = 0)
        x_rec = torch.concat(x_rec, dim = 0)
        # del x_i, x_subj, x_i_rec

        # forward cycling
        x_b = self.builder[str(subj_b)](x_a)  # subj_b recon brain signal using subj_a seman embedding
        x_b = self.embedder[str(subj_b)](x_b)  # subj_b seman embedding (pseudo)
        loss_cyc = self.cyc_loss(x_a, x_b)

        x = self.translator(x)

        x_image = self.head_image(x)

        return x_image, x_rec, loss_cyc


class MindSingle_image_sketch(nn.Module):
    def __init__(
        self,
        in_dim = 15724,
        out_dim_image_feature_map = 768,
        out_dim_image_fc = None,
        h = 4096,
        n_blocks = 4,
        subj_list = None,
    ):

        super().__init__()

        self.subj_list = subj_list
        self.embedder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    Adapter_Layer(in_dim, 128), nn.Linear(in_dim, h), nn.LayerNorm(h), nn.GELU(), nn.Dropout(0.5),
                ) for subj in subj_list
            }
        )

        self.translator = ResMLP(h, n_blocks)
        self.head_image_2 = nn.Linear(h, out_dim_image_feature_map)
        self.head_image_3 = nn.Linear(h, out_dim_image_feature_map)
        self.head_image_4 = nn.Linear(h, out_dim_image_feature_map)
        self.head_image_5 = nn.Linear(h, out_dim_image_feature_map)
        self.head_image_6 = nn.Linear(h, out_dim_image_feature_map)
        self.head_image_7 = nn.Linear(h, out_dim_image_feature_map)

    # @torchsnooper.snoop()
    def forward(self, x):
        x = self.embedder[str(self.subj_list[0])](x)
        x = self.translator(x)

        x_image_2 = self.head_image_2(x)
        x_image_3 = self.head_image_3(x)
        x_image_4 = self.head_image_4(x)
        x_image_5 = self.head_image_5(x)
        x_image_6 = self.head_image_6(x)
        x_image_7 = self.head_image_7(x)

        return x_image_2, x_image_3, x_image_4, x_image_5, x_image_6, x_image_7


class MindBridge_image_sketch(MindSingle_image_sketch):
    def __init__(
        self,
        in_dim = 15724,
        out_dim_image_feature_map = 768,
        out_dim_image_fc = None,
        h = 4096,
        n_blocks = 4,
        subj_list = None,
        adapting = False
    ):

        assert len(subj_list) >= 2, "MindBridge requires at least 2 subjects"

        super().__init__(
            in_dim = in_dim,
            out_dim_image_feature_map = out_dim_image_feature_map,
            out_dim_image_fc = out_dim_image_fc,
            h = h,
            n_blocks = n_blocks,
            subj_list = subj_list
        )

        self.builder = nn.ModuleDict(
            {
                str(subj): nn.Sequential(
                    nn.Linear(h, in_dim), nn.LayerNorm(in_dim), nn.GELU(), Adapter_Layer(in_dim, 128),
                ) for subj in subj_list
            }
        )

        self.adapting = adapting
        self.cyc_loss = nn.MSELoss()

    # @torchsnooper.snoop()
    def forward(self, x):
        if len(x) == 2 and type(x) is tuple:
            subj_list = x[1].tolist()  # (s,)
            x = x[0]  # (b,n)
        else:
            subj_list = self.subj_list

        x = x.squeeze()
        x_subj = torch.chunk(x, len(subj_list))
        x = []
        x_rec = []
        if self.adapting:  # choose subj_a (source subject) and subj_b (target subject)
            subj_a, subj_b = subj_list[0], subj_list[-1]
        else:  # random sample 2 subjects
            subj_a, subj_b = random.sample(subj_list, 2)
        for i, subj_i in enumerate(subj_list):  # subj is 1-based
            x_i = self.embedder[str(subj_i)](x_subj[i])  # subj_i seman embedding
            if subj_i == subj_a: x_a = x_i  # subj_a seman embedding are choosen
            x.append(x_i)
            x_i_rec = self.builder[str(subj_i)](x_i)  # subj_i recon brain signals
            x_rec.append(x_i_rec)

        x = torch.concat(x, dim = 0)
        x_rec = torch.concat(x_rec, dim = 0)
        # del x_i, x_subj, x_i_rec

        # forward cycling
        x_b = self.builder[str(subj_b)](x_a)  # subj_b recon brain signal using subj_a seman embedding
        x_b = self.embedder[str(subj_b)](x_b)  # subj_b seman embedding (pseudo)
        loss_cyc = self.cyc_loss(x_a, x_b)

        x = self.translator(x)

        x_image_2 = self.head_image_2(x)
        x_image_3 = self.head_image_3(x)
        x_image_4 = self.head_image_4(x)
        x_image_5 = self.head_image_5(x)
        x_image_6 = self.head_image_6(x)
        x_image_7 = self.head_image_7(x)

        return x_image_2, x_image_3, x_image_4, x_image_5, x_image_6, x_image_7, x_rec, loss_cyc
