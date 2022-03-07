_base_ = [
    '../../_base_/models/denseclip_r50.py', '../../_base_/datasets/web_image.py', 
    '../../_base_/default_runtime.py', '../../_base_/schedules/schedule_20k.py'
]
img_dir = 'batman'
num_class = 7
model = dict(
    decode_head=dict(
        num_classes=num_class,
        text_categories=num_class,
        text_channels=1024,
        text_embeddings_path=f'pretrain/{img_dir}_RN50_clip_text.pth',
        visual_projs_path='pretrain/RN50_clip_weights.pth',
    )
)
data = dict(
    samples_per_gpu=4,
    train=dict(img_dir=img_dir),
    val=dict(img_dir=img_dir),
    test=dict(img_dir=img_dir, split=f'{img_dir}.txt')
)