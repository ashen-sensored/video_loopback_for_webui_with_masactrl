import gradio as gr
import modules
import time
from modules import processing, shared
from modules.processing import Processed

import os, math, json
from PIL import Image, ImageChops, ImageFilter
from collections import deque
from pathlib import Path
from typing import List, Tuple, Iterable

from modules import scripts

from scripts.video_loopback_utils import utils
from scripts.video_loopback_utils.utils import \
    resize_img, make_video, is_image, get_image_paths, \
    get_prompt_for_images, blend_average, get_now_time
from scripts.video_loopback_utils.fastdvdnet_processor import FastDVDNet

from extensions.sd_webui_masactrl.scripts.masactrl_controller import MasaControllerMode



def update_script_args(p, value, arg_idx, script_class):

    for s in scripts.scripts_txt2img.alwayson_scripts:
        if isinstance(s, script_class):
            args = list(p.script_args)
            # print(f"Changed arg {arg_idx} from {args[s.args_from + arg_idx - 1]} to {value}")
            args[s.args_from + arg_idx] = value
            p.script_args = tuple(args)
            break
def gr_show(visible=True):
    return {"visible": visible, "__type__": "update"}


class TemporalImageBlender:
    def __init__(
            self, image_path_list=None, window_size=1,
            target_size=(512, 512),
            use_mask=False,
            mask_dir='', mask_threshold=127):
        self.image_path_list = image_path_list
        self.target_size = target_size

        assert window_size % 2 == 1
        self.window_size = window_size
        self.window = deque(
            self.read_image_resize(image_path_list[i])
            for i in range(window_size // 2 + 1)
        )
        self.current_i = 0
        self.current_pos = 0

        self.use_mask = use_mask
        self.mask_dir = Path(mask_dir) if mask_dir else None
        self.mask_threshold = mask_threshold

    def read_image_resize(self, path) -> Image.Image:
        return resize_img(Image.open(path), self.target_size)

    def move_to_next(self):
        hws = self.window_size // 2  # half window size
        self.current_i += 1
        if self.current_i >= len(self.image_path_list):
            self.current_i = len(self.image_path_list) - 1
            return
        if self.current_i + hws < len(self.image_path_list):
            self.window.append(self.read_image_resize(
                self.image_path_list[self.current_i + hws]))
        if self.current_i - hws > 0:
            self.window.popleft()
        else:
            self.current_pos += 1
        assert self.current_pos < len(self.window) <= self.window_size

    def reset(self):
        self.window = deque(
            self.read_image_resize(self.image_path_list[i])
            for i in range(self.window_size // 2 + 1)
        )
        self.current_i = 0
        self.current_pos = 0

    def current_image(self) -> Image.Image:
        return self.window[self.current_pos]

    def current_mask(self):
        if not self.use_mask:
            return None
        mask = None
        if self.mask_dir:
            if self.mask_dir.is_dir():
                mask_name = self.image_path_list[self.current_i].name
                mask_path = self.mask_dir/mask_name
                if mask_path.is_file():
                    mask = Image.open(mask_path).convert('L')
                else:
                    print(f'Warning: "{mask_path}" has no mask')
            elif self.mask_dir.is_file():
                mask = Image.open(self.mask_dir).convert('L')
            else:
                raise FileNotFoundError("mask not found")
        else:
            if 'RGBA' == self.current_image().mode:
                mask = self.current_image().split()[-1]
            else:
                print('current image mode: ', self.current_image().mode)
                print(f'Warning: "{self.image_path_list[self.current_i]}" has no alpha mask')
        # apply threshold
        if mask is not None:
            mask = mask.convert('L').point(
                lambda x: 255 if x > self.mask_threshold else 0)

            # resize mask
            if mask.size != self.target_size:
                mask = resize_img(mask, self.target_size)

        return mask

    def blend_batch(self, new_imgs: Iterable[Image.Image],
                    superimpose_alpha, mask=None):
        if not new_imgs:
            return self.current_image()

        new_img: Image.Image = blend_average(new_imgs)
        base_img = self.current_image().convert(new_img.mode)
        new_img = resize_img(new_img, base_img.size)  # SD输出尺寸可能与用户指定尺寸不同
        output_img = Image.blend(base_img, new_img, superimpose_alpha)

        if mask is None:
            mask = self.current_mask()
        if mask:
            output_img = Image.composite(output_img, base_img, mask)

        return output_img

    def blend_temporal(self, alpha_list, mask=None):
        if len(alpha_list) != self.window_size:
            raise ValueError('the length of temporal_superimpose_alpha_list must be fixed')
        hws = self.window_size // 2  # half window size
        now_fac_sum = 0.0
        output_img = self.window[0]
        for factor, img in zip(
                alpha_list[-(self.current_i + hws + 1):],
                self.window):
            now_fac_sum += factor
            if now_fac_sum <= 0:
                continue
            img = img.convert(output_img.mode)
            output_img = Image.blend(output_img, img, factor / now_fac_sum)

        if mask is None:
            mask = self.current_mask()
        if mask:
            output_img = Image.composite(output_img, self.current_image(), mask)
            # 当mask像素取255时为img1,取0时为img2

        return output_img

    def blend_temporal_diff(self, alpha_list, reference_img_list, mask=None):
        if len(alpha_list) != self.window_size:
            raise ValueError('the length of temporal_superimpose_alpha_list must be fixed')
        hws = self.window_size // 2  # half window size
        output_img = blend_average(
            Image.composite(
                self.current_image(), img,
                mask=ImageChops.difference(
                    reference_img_list[self.current_pos],
                    origin_img
                ).convert('L').point(
                    lambda x: 255 if x > 0 else 255*(1-alpha)
                ).resize(img.size,  Image.ANTIALIAS)
            )
            for alpha, img, origin_img in zip(
                alpha_list[-(self.current_i + hws + 1):],
                self.window,
                reference_img_list
            )
        )

        if mask is None:
            mask = self.current_mask()
        if mask:
            output_img = Image.composite(output_img, self.current_image(), mask)
            # 当mask像素取255时为img1,取0时为img2

        return output_img

    def save_current_output_image(self, path, img: Image.Image):
        max_retries = 3
        retry_interval = 5  # seconds
        for i in range(max_retries):
            try:
                if self.use_mask and not self.mask_dir:
                    img.putalpha(self.current_mask())
                img.save(path)
                break 
            except (OSError, FileNotFoundError) as e:
                # Transport endpoint is not connected or FileNotFoundError
                print(f"Exception occurred: {type(e).__name__} - {str(e)}")
                if i < max_retries - 1:  # wait for a while unless this is the last try
                    time.sleep(retry_interval)
                else:
                    raise 

class Script(modules.scripts.Script):
    def title(self):
        return "Video Loopback"

    def show(self, is_img2img):
        return is_img2img

    def ui(self, is_img2img):
        input_dir = gr.Textbox(
            label='input_directory',
            placeholder='A directory or a file'
        )
        output_dir = gr.Textbox(label='output_directory')
        # mask settings
        use_mask = gr.Checkbox(label='use_mask(inpainting)', value=False)
        with gr.Box(visible=False) as mask_settings_box:
            mask_dir = gr.Textbox(
                label='mask_directory',
                value='',
                placeholder='A directory or a file. '
                            'Keep this empty to use the alpha channel of image as mask'
            )
            # use_alpha_as_mask = gr.Checkbox(label='use_alpha_as_mask', value=False)
            mask_threshold = gr.Slider(label='mask_threshold', minimum=0, maximum=255, step=1, value=127)
        use_mask.change(
            fn=lambda x: gr_show(x),
            show_progress=False,
            inputs=[use_mask], outputs=[mask_settings_box]
        )
        read_prompt_from_txt = gr.Checkbox(label='read_prompt_from_txt', value=False)
        output_frame_rate = gr.Number(label='output_frame_rate', precision=0, value=30)
        max_frames = gr.Number(label='max_frames', precision=0, value=9999)
        extract_nth_frame = gr.Number(label='extract_nth_frame', precision=0, value=1)
        is_continuous = gr.Checkbox(
            label='is_continuous (ignore the "extract_nth_frame" for input frames only)', value=False
        )
        loop_n = gr.Number(label='loop_n', precision=0, value=10)
        superimpose_alpha = gr.Slider(label='superimpose_alpha', minimum=0, maximum=1, step=0.01, value=0.25)
        fix_seed = gr.Checkbox(label='fix_seed', value=True)
        fix_subseed = gr.Checkbox(label='fix_subseed', value=False)
        temporal_superimpose_method = gr.Dropdown(
            label='temporal_superimpose_method',
            choices=['simple', 'with difference mask from reference'],
            value='simple'
        )
        temporal_superimpose_alpha_list = gr.Textbox(
            label='temporal_superimpose_alpha_list',
            value='1',
            placeholder='0.03,0.95,0.02'
        )
        reference_frames_dir = gr.Textbox(
            label='reference_frames_directory',
            placeholder='Reference frames for temporal superimpose and Controlnet. '
                        'Keep this empty to use the input frames as reference. '
                        'Split you paths with "!!!" if you are using multi-Controlnet. '
        )
        save_every_loop = gr.Checkbox(label='save_every_loop', value=True)

        # MASAControl settings
        masa_control_use_index = gr.Checkbox(label='masa_control_use_index', value=False)
        masa_control_active_range = gr.Textbox(
            label='masa_control_active_range',
            value='0-100,102-110;135-145',
            placeholder='Example: 0,0.5'
        )

        # extra settings
        with gr.Accordion('**Advanced Settings of Video Loopback:**', open=True):
            gr.Markdown(
                "You can use any python expression in your schedule <br>"
                "Available parameters: math.*, image_i, loop_i, PIL.ImageFilter <br>"
                "If seed_schedule/subseed_schedule is not empty, fix_seed/fix_subseed is ignored <br>"
                "These examples are just to demonstrate usage and are not recommended parameters."
            )
            subseed_strength_schedule = gr.Textbox(
                label='subseed_strength_schedule',
                placeholder='Example: (sin(pi*image_i/90)+1)/2*0.07'
            )
            denoising_schedule = gr.Textbox(
                label='denoising_schedule',
                placeholder='Example: 0.4 if loop_i<3 else 0.3'
            )
            step_schedule = gr.Textbox(
                label='step_schedule',
                placeholder='Example: 10 if loop_i<3 else 20'
            )
            seed_schedule = gr.Textbox(
                label='seed_schedule',
                placeholder='Example: [111,222,333][image_i//5%3]'
            )
            subseed_schedule = gr.Textbox(
                label='subseed_schedule',
                placeholder='Example: 112233+image_i*2'
            )
            cfg_schedule = gr.Textbox(
                label='cfg_schedule',
                placeholder='Example: 7 if image_i in {1,5,7} else 8'
            )
            superimpose_alpha_schedule = gr.Textbox(
                label='superimpose_alpha_schedule',
                placeholder='Example: 0.3 if loop_i<3 else 0.2 if loop_i<5 else 0.1'
            )
            temporal_superimpose_schedule = gr.Textbox(
                label='temporal_superimpose_schedule',
                placeholder='Example: [0.1, 0.8, 0.1] if loop_i<=3 else [0.0, 1.0, 0.0]'
            )
            prompt_schedule = gr.Textbox(
                label='prompt_schedule',
                placeholder="Example: ['1girl,smile','1girl,closed mouth'][image_i//10%2]"
            )
            negative_prompt_schedule = gr.Textbox(
                label='negative_prompt_schedule',
                placeholder="Example: f' low quality, (blurry:{1.0+loop_i/30})'"
            )
            batch_count_schedule = gr.Textbox(
                label='batch_count_schedule',
                placeholder="Example: 5 if loop_i<=5 else 1"
            )
            image_post_processing_schedule = gr.Textbox(
                label='image_post_processing_schedule',
                placeholder="Example: "
                            "lambda img: img.filter(ImageFilter.EDGE_ENHANCE).filter(ImageFilter.SMOOTH) "
                            "if loop_i in {6,8} else img "
            )
            video_post_process_method = gr.Dropdown(
                label='video_post_process_method',
                choices=['None', 'FastDVDNet'],
                value='None'
            )
            video_post_process_alpha = gr.Slider(
                label='video_post_process_alpha',
                minimum=0, maximum=1, step=0.01, value=0.3
            )
            with gr.Box(visible=False) as video_post_process_fastdvdnet_box:
                fastdvdnet_noise_sigma = gr.Slider(
                    label='fastdvdnet_noise_sigma',
                    minimum=0, maximum=255, step=1, value=60
                )
            video_post_process_method.change(
                lambda x: gr_show(x == 'FastDVDNet'),
                show_progress=False,
                inputs=[video_post_process_method],
                outputs=[video_post_process_fastdvdnet_box]
            )

        return [
            input_dir,
            output_dir,
            use_mask,
            mask_dir,
            mask_threshold,
            read_prompt_from_txt,
            output_frame_rate,
            max_frames,
            extract_nth_frame,
            is_continuous,
            loop_n,
            superimpose_alpha,
            fix_seed,
            fix_subseed,
            temporal_superimpose_method,
            temporal_superimpose_alpha_list,
            reference_frames_dir,
            save_every_loop,
            masa_control_use_index,
            masa_control_active_range,
            subseed_strength_schedule,
            denoising_schedule,
            step_schedule,
            seed_schedule,
            subseed_schedule,
            cfg_schedule,
            superimpose_alpha_schedule,
            temporal_superimpose_schedule,
            prompt_schedule,
            negative_prompt_schedule,
            batch_count_schedule,
            image_post_processing_schedule,
            video_post_process_method,
            video_post_process_alpha,
            fastdvdnet_noise_sigma
        ]

    def run(self, p,
            input_dir,
            output_dir,
            use_mask,
            mask_dir,
            mask_threshold,
            read_prompt_from_txt,
            output_frame_rate,
            max_frames,
            extract_nth_frame,
            is_continuous,
            loop_n,
            superimpose_alpha,
            fix_seed,
            fix_subseed,
            temporal_superimpose_method,
            temporal_superimpose_alpha_list,
            reference_frames_dir,
            save_every_loop,
            masa_control_use_index,
            masa_control_active_range,
            subseed_strength_schedule,
            denoising_schedule,
            step_schedule,
            seed_schedule,
            subseed_schedule,
            cfg_schedule,
            superimpose_alpha_schedule,
            temporal_superimpose_schedule,
            prompt_schedule,
            negative_prompt_schedule,
            batch_count_schedule,
            image_post_processing_schedule,
            video_post_process_method,
            video_post_process_alpha,
            fastdvdnet_noise_sigma):

        processing.fix_seed(p)
        p.do_not_save_grid = True
        p.do_not_save_samples = True
        processed = None

        timestamp = get_now_time()

        if not input_dir:
            raise ValueError('input_dir is empty')
        if not output_dir:
            raise ValueError('output_dir is empty')

        utils.resize_mode = p.resize_mode

        # save settings
        args_dict = {
            "timestamp": timestamp,
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "use_mask": use_mask,
            "mask_dir": mask_dir,
            "mask_threshold": mask_threshold,
            "read_prompt_from_txt": read_prompt_from_txt,
            "output_frame_rate": output_frame_rate,
            "max_frames": max_frames,
            "extract_nth_frame": extract_nth_frame,
            "is_continuous": is_continuous,
            "loop_n": loop_n,
            "superimpose_alpha": superimpose_alpha,
            "fix_seed": fix_seed,
            "fix_subseed": fix_subseed,
            "temporal_superimpose_method": temporal_superimpose_method,
            "temporal_superimpose_alpha_list": temporal_superimpose_alpha_list,
            "reference_frames_dir": reference_frames_dir,
            "save_every_loop": save_every_loop,
            "masa_control_use_index": masa_control_use_index,
            "masa_control_active_range": masa_control_active_range,
            "subseed_strength_schedule": subseed_strength_schedule,
            "denoising_schedule": denoising_schedule,
            "step_schedule": step_schedule,
            "seed_schedule": seed_schedule,
            "subseed_schedule": subseed_schedule,
            "cfg_schedule": cfg_schedule,
            "superimpose_alpha_schedule": superimpose_alpha_schedule,
            "temporal_superimpose_schedule": temporal_superimpose_schedule,
            "prompt_schedule": prompt_schedule,
            "negative_prompt_schedule": negative_prompt_schedule,
            "batch_count_schedule": batch_count_schedule,
            "image_post_processing_schedule": image_post_processing_schedule,
            "video_post_process_method": video_post_process_method,
            "video_post_process_alpha": video_post_process_alpha,
            "fastdvdnet_noise_sigma": fastdvdnet_noise_sigma,

            # "p": p.__dict__
            "seed": p.seed,
            "subseed": p.subseed,
            "subseed_strength": p.subseed_strength,
            "cfg_scale": p.cfg_scale,
            "prompt": p.prompt,
            "negative_prompt": p.negative_prompt,
            "sampler_name": p.sampler_name,
            "width": p.width,
            "height": p.height,
            "denoising_strength": getattr(p, 'denoising_strength', None),
            "batch_size": p.batch_size,
            "n_iter": p.n_iter,
            "steps": p.steps,
            "resize_mode": p.resize_mode,
            "clip_skip": shared.opts.CLIP_stop_at_last_layers,
            "model_name": shared.sd_model.sd_checkpoint_info.model_name,
            "model_hash": shared.sd_model.sd_model_hash
        }

        output_dir = Path(output_dir) / timestamp
        output_frames_dir = output_dir/"output_frames"
        output_frames_dir.mkdir(exist_ok=True, parents=True)

        settings_file_name = f'{timestamp}.json'
        with open(output_dir/settings_file_name, 'w', encoding='utf-8') as f:
            json.dump(args_dict, f, indent=4, ensure_ascii=False)

        input_dir = Path(input_dir)
        assert input_dir.exists()
        if input_dir.is_file() and not is_image(input_dir):  # 输入为视频文件
            extract_dir = output_dir / 'input_frames'
            extract_dir.mkdir()
            os.system(f'ffmpeg -i "{input_dir}" "{extract_dir / "%07d.png"}" ')
            input_dir = extract_dir

        if is_image(input_dir):  # 输入为单张图片
            image_list = [input_dir] * max_frames
        else:
            image_list = get_image_paths(input_dir)
            if not is_continuous:
                image_list = image_list[::extract_nth_frame]
            image_list = image_list[:max_frames]
        image_n = len(image_list)

        temporal_superimpose_alpha_list = \
            [float(x) for x in temporal_superimpose_alpha_list.split(',') if x
             ] or [1]
        assert len(temporal_superimpose_alpha_list) % 2 == 1

        # make video_post_processor
        video_post_processor = None
        if 'FastDVDNet' == video_post_process_method:
            print('using FastDVDNet as video post processor')
            video_post_processor = FastDVDNet(
                alpha=video_post_process_alpha,
                noise_sigma=fastdvdnet_noise_sigma
            )

        shared.state.begin()
        shared.state.job_count = loop_n * image_n * p.n_iter

        schedule_args = dict(ImageFilter=ImageFilter, **math.__dict__)

        if read_prompt_from_txt:
            default_prompt = p.prompt
            default_neg_prompt = p.negative_prompt
            prompt_list = get_prompt_for_images(image_list)

        # init references
        if not reference_frames_dir:
            reference_image_list = [image_list]
        else:
            reference_image_list = [
                get_image_paths(Path(p))[::extract_nth_frame][:max_frames]
                for p in reference_frames_dir.split('!!!') if p
            ]  # 可能为空
        reference_img_ques = [
            TemporalImageBlender(
                image_path_list=image_list,
                window_size=len(temporal_superimpose_alpha_list),
                target_size=(p.width, p.height),
                use_mask=use_mask, mask_dir=mask_dir,
                mask_threshold=mask_threshold
            )
            for image_list in reference_image_list
        ]


        def parse_ranges(text):
            sections = text.split(';')
            sections_list = []
            for section in sections:
                intervals_list = []
                intervals = section.split(',')
                for interval in intervals:
                    start, end = map(int, interval.split('-'))
                    intervals_list.append([start, end])
                sections_list.append(intervals_list)
            return sections_list
        masa_ctrl_logging_list = []
        masa_ctrl_logrecon_list = []

        if masa_control_active_range != "":
            masa_ctrl_sections_list = parse_ranges(masa_control_active_range)

            for section in masa_ctrl_sections_list:
                intervals_list = section
                masa_ctrl_logging_list.append(intervals_list[0][0])
                for i, interval in enumerate(intervals_list):
                    if not i == 0:
                        masa_ctrl_logrecon_list.append(interval[0])
                    masa_ctrl_logrecon_list.extend(list(range(interval[0]+1, interval[1]+1)))





        for loop_i in range(loop_n):
            if shared.state.interrupted:
                break

            if loop_i > 0:
                image_list = get_image_paths(output_frames_dir)
            output_frames_dir = output_dir/"output_frames"/f"loop_{loop_i+1}"
            output_frames_dir.mkdir()

            img_que = TemporalImageBlender(
                image_path_list=image_list,
                window_size=len(temporal_superimpose_alpha_list),
                target_size=(p.width, p.height),
                use_mask=use_mask, mask_dir=mask_dir,
                mask_threshold=mask_threshold
            )

            for image_i, image_path in enumerate(image_list):
                if shared.state.interrupted:
                    break

                print('='*10)
                print(f"Loop:{loop_i + 1}/{loop_n},Image:{image_i + 1}/{image_n}")
                # shared.state.job = f"Loop:{loop_i + 1}/{loop_n},Image:{image_i + 1}/{image_n}"

                output_filename = output_frames_dir / f"{image_i:07d}.png"

                # do all schedule
                schedule_args.update({'image_i': image_i+1, 'loop_i': loop_i+1})

                target_masactrl_script_object = next(
                    (v for v in scripts.scripts_img2img.scripts if str(v).startswith('<masactrl_ui.py.Script')), None)

                if masa_control_use_index:
                    input_img_stem = image_i
                else:
                    input_img_stem = int(Path(image_path).stem)

                if masa_control_active_range != "":
                    if input_img_stem in masa_ctrl_logging_list:
                        update_script_args(p, MasaControllerMode.LOGGING,0,target_masactrl_script_object.__class__)
                    elif input_img_stem in masa_ctrl_logrecon_list:
                        update_script_args(p, MasaControllerMode.LOGRECON,0,target_masactrl_script_object.__class__)
                    else:
                        update_script_args(p, MasaControllerMode.IDLE,0,target_masactrl_script_object.__class__)

                if subseed_strength_schedule:
                    p.subseed_strength = eval(subseed_strength_schedule, schedule_args)
                    print(f"subseed_strength_schedule:{p.subseed_strength}")
                if denoising_schedule:
                    p.denoising_strength = eval(denoising_schedule, schedule_args)
                    print(f"denoising_schedule:{p.denoising_strength}")
                if step_schedule:
                    p.steps = eval(step_schedule, schedule_args)
                    print(f"step_schedule:{p.steps}")
                if seed_schedule:
                    p.seed = eval(seed_schedule, schedule_args)
                    print(f"seed_schedule:{p.seed}")
                if subseed_schedule:
                    p.subseed = eval(subseed_schedule, schedule_args)
                    print(f"subseed_schedule:{p.subseed}")
                if cfg_schedule:
                    p.cfg_scale = eval(cfg_schedule, schedule_args)
                    print(f"cfg_schedule:{p.cfg_scale}")
                if superimpose_alpha_schedule:
                    superimpose_alpha = eval(superimpose_alpha_schedule, schedule_args)
                    print(f"superimpose_alpha_schedule:{superimpose_alpha}")
                if temporal_superimpose_schedule:
                    new_temporal_superimpose_list = eval(temporal_superimpose_schedule, schedule_args)
                    assert len(temporal_superimpose_alpha_list) == len(new_temporal_superimpose_list)
                    temporal_superimpose_alpha_list = new_temporal_superimpose_list
                    print(f"temporal_superimpose_schedule:{temporal_superimpose_alpha_list}")
                if prompt_schedule:
                    p.prompt = eval(prompt_schedule, schedule_args)
                    print(f"prompt_schedule:{p.prompt}")
                if negative_prompt_schedule:
                    p.negative_prompt = eval(negative_prompt_schedule, schedule_args)
                    print(f"negative_prompt_schedule:{p.negative_prompt}")
                if batch_count_schedule:
                    new_batch_count = eval(batch_count_schedule, schedule_args)
                    if isinstance(new_batch_count, tuple):
                        p.n_iter, p.batch_size = new_batch_count
                        print(f"batch_count_schedule: batch_count:{p.n_iter}, batch_size:{p.batch_size}")
                    else:
                        p.n_iter = new_batch_count
                        print(f"batch_count_schedule:{p.n_iter}")

                image_post_processing = None
                if image_post_processing_schedule:
                    image_post_processing = eval(image_post_processing_schedule, schedule_args)
                    print(f"image_post_processing_schedule:{image_post_processing_schedule}")

                if read_prompt_from_txt:
                    prompt, neg_prompt = prompt_list[image_i]
                    if prompt is not None:
                        p.prompt = prompt
                    elif not prompt_schedule:
                        p.prompt = default_prompt
                    if neg_prompt is not None:
                        p.negative_prompt = neg_prompt
                    elif not negative_prompt_schedule:
                        p.negative_prompt = default_neg_prompt
                    print(f"prompt: {p.prompt} \n"
                          f"negative prompt: {p.negative_prompt}")

                # make base img for i2i
                if "with difference mask from reference" == temporal_superimpose_method:
                    if len(reference_img_ques) <= 0:
                        raise ValueError('Current temporal superimpose method need reference')
                    base_img = img_que.blend_temporal_diff(
                        temporal_superimpose_alpha_list,
                        reference_img_list=reference_img_ques[0].window
                    )
                else:
                    base_img = img_que.blend_temporal(temporal_superimpose_alpha_list)

                print(f"seed:{p.seed}, subseed:{p.subseed}")

                p.init_images = [base_img]
                p.image_mask = img_que.current_mask()
                # mask像素为0表示不变

                # 使用 sd-webui-controlnet
                p.control_net_input_image = [
                    que.current_image()
                    for que in reference_img_ques
                ]

                processed = processing.process_images(p)

                # masactrl post process
                if masa_control_active_range != "":
                    if input_img_stem in masa_ctrl_logging_list + masa_ctrl_logrecon_list:
                        shared.masa_controller.calculate_reconstruction_maps()

                processed_imgs = processed.images
                processed_imgs = [
                    img for img in processed_imgs
                    if isinstance(img, Image.Image)
                ][:p.n_iter*p.batch_size]

                # batch blend
                output_img = img_que.blend_batch(
                        processed_imgs, superimpose_alpha)

                if image_post_processing:
                    output_img = image_post_processing(output_img)

                # output_img.save(output_filename)
                img_que.save_current_output_image(output_filename, output_img)

                img_que.move_to_next()
                for que in reference_img_ques:
                    que.move_to_next()

                if not fix_seed and not seed_schedule:
                    p.seed = processed.seed + p.n_iter * p.batch_size
                if not fix_subseed and not subseed_schedule:
                    p.subseed = processed.subseed + p.n_iter * p.batch_size

            # post process
            if video_post_processor is not None:
                video_post_processor.process(output_frames_dir)

            if save_every_loop:
                output_video_name = f'{timestamp}-loop_{loop_i+1}.mp4'
                make_video(
                    input_dir=output_frames_dir,
                    output_filename=output_dir/output_video_name,
                    frame_rate=output_frame_rate
                )

            for que in reference_img_ques:
                que.reset()

        output_video_name = f'{timestamp}.mp4'
        make_video(
            input_dir=output_frames_dir,
            output_filename=output_dir / output_video_name,
            frame_rate=output_frame_rate
        )

        print(f"\n {timestamp} finished! now time:{get_now_time()}\n")
        shared.state.end()

        return processed
