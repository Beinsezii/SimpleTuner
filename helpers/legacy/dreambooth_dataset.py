from torch.utils.data import Dataset
from pathlib import Path
from torchvision import transforms
from PIL.ImageOps import exif_transpose
from ..training.state_tracker import StateTracker
from PIL import Image
import json, logging, os, multiprocessing
from tqdm import tqdm
from multiprocessing import Pool, cpu_count, Manager, Value, Lock, Process, Queue
import numpy as np
from itertools import repeat
from ctypes import c_int

logger = logging.getLogger("DatasetLoader")
target_level = os.environ.get("SIMPLETUNER_LOG_LEVEL", "WARNING")
logger.setLevel(target_level)
from concurrent.futures import ThreadPoolExecutor
import threading

pil_logger = logging.getLogger("PIL.Image")
pil_logger.setLevel(logging.WARNING)
pil_logger = logging.getLogger("PIL.PngImagePlugin")
pil_logger.setLevel(logging.WARNING)

multiprocessing.set_start_method("fork")


class DreamBoothDataset(Dataset):
    """
    A dataset to prepare the instance and class images with the prompts for fine-tuning the model.
    It pre-processes the images and the tokenizes prompts.
    """

    def __init__(
        self,
        instance_data_root,
        accelerator,
        instance_prompt: str = None,
        tokenizer=None,
        aspect_ratio_buckets=[1.0, 1.5, 0.67, 0.75, 1.78],
        size=1024,
        center_crop=False,
        print_names=False,
        use_captions=True,
        prepend_instance_prompt=False,
        use_original_images=False,
        caption_dropout_interval: int = 0,
        use_precomputed_token_ids: bool = True,
        debug_dataset_loader: bool = False,
        caption_strategy: str = "filename",
    ):
        self.prepend_instance_prompt = prepend_instance_prompt
        self.use_captions = use_captions
        self.size = size
        self.center_crop = center_crop
        self.tokenizer = tokenizer
        self.print_names = print_names
        self.debug_dataset_loader = debug_dataset_loader
        self.instance_data_root = Path(instance_data_root)
        if not self.instance_data_root.exists():
            raise ValueError(
                f"Instance {self.instance_data_root} images root doesn't exists."
            )
        self.instance_images_path = list(Path(instance_data_root).iterdir())
        self.num_instance_images = len(self.instance_images_path)
        self.instance_prompt = instance_prompt
        self.aspect_ratio_buckets = aspect_ratio_buckets
        self.use_original_images = use_original_images
        self.accelerator = accelerator
        self.aspect_ratio_bucket_indices = {}
        self.aspect_ratio_bucket_indices = self.assign_to_buckets()
        self.caption_dropout_interval = caption_dropout_interval
        self.caption_loop_count = 0
        self.caption_strategy = caption_strategy
        self.use_precomputed_token_ids = use_precomputed_token_ids
        self._length = self.num_instance_images
        if not use_original_images:
            logger.debug(f"Building transformations.")
            self.image_transforms = self._get_image_transforms()

    def _get_image_transforms(self):
        return transforms.Compose(
            [
                transforms.Resize(
                    self.size, interpolation=transforms.InterpolationMode.BILINEAR
                ),
                transforms.CenterCrop(self.size)
                if self.center_crop
                else transforms.RandomCrop(self.size),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ]
        )

    def _process_image(self, image_path_str, aspect_ratio_bucket_indices):
        try:
            image = Image.open(image_path_str)
            # Apply EXIF transforms
            image = exif_transpose(image)
            aspect_ratio = round(
                image.width / image.height, 2
            )  # Round to avoid excessive unique buckets
            # Create a new bucket if it doesn't exist
            if str(aspect_ratio) not in aspect_ratio_bucket_indices:
                aspect_ratio_bucket_indices[str(aspect_ratio)] = []
            aspect_ratio_bucket_indices[str(aspect_ratio)].append(image_path_str)
        except Exception as e:
            logger.error(f"Error processing image {image_path_str}.")
            logger.error(e)
            return aspect_ratio_bucket_indices
        finally:
            if "image" in locals():
                image.close()
        return aspect_ratio_bucket_indices

    def _add_file_to_cache(self, file_path):
        """Add a single file to the cache (thread-safe)."""
        try:
            with Image.open(file_path) as image:
                # Apply EXIF transforms
                image = exif_transpose(image)
                aspect_ratio = round(
                    image.width / image.height, 2
                )  # Round to avoid excessive unique buckets

                with threading.Lock():
                    # Create a new bucket if it doesn't exist
                    if str(aspect_ratio) not in self.aspect_ratio_bucket_indices:
                        self.aspect_ratio_bucket_indices[str(aspect_ratio)] = []
                    self.aspect_ratio_bucket_indices[str(aspect_ratio)].append(
                        file_path
                    )
        except Exception as e:
            logger.error(f"Error processing image {file_path}.")
            logger.error(e)

    def load_aspect_ratio_bucket_indices(self, cache_file):
        logger.info("Loading aspect ratio bucket indices from cache file.")
        with cache_file.open("r") as f:
            try:
                aspect_ratio_bucket_indices = json.load(f)
            except:
                logger.warn(
                    f"Could not load aspect ratio bucket indices from {cache_file}. Creating a new one!"
                )
                aspect_ratio_bucket_indices = {}
        logger.info("Loading of aspect bucket indexes completed.")
        return aspect_ratio_bucket_indices

    def _bucket_worker(
        self, tqdm_queue, files, aspect_ratio_bucket_indices_queue, existing_files_set
    ):
        for file in files:
            if str(file) in existing_files_set:
                tqdm_queue.put(1)  # Update progress bar but skip further processing
                continue
            # Process image and send results to queue as before
            aspect_ratio_bucket_indices = self._process_image(
                str(file), self.aspect_ratio_bucket_indices
            )
            tqdm_queue.put(1)  # Update progress bar
            aspect_ratio_bucket_indices_queue.put(aspect_ratio_bucket_indices)

    def compute_aspect_ratio_bucket_indices(self, cache_file):
        logger.warning("Computing aspect ratio bucket indices.")

        # Step 1: Initialization Check
        if (
            hasattr(self, "aspect_ratio_bucket_indices")
            and self.aspect_ratio_bucket_indices
        ):
            aspect_ratio_bucket_indices = self.aspect_ratio_bucket_indices
        else:
            aspect_ratio_bucket_indices = {}

        def rglob_follow_symlinks(path: Path, pattern: str):
            for p in path.glob(pattern):
                yield p
            for p in path.iterdir():
                if p.is_dir() and not p.is_symlink():
                    yield from rglob_follow_symlinks(p, pattern)
                elif p.is_symlink():
                    real_path = Path(os.readlink(p))
                    if real_path.is_dir():
                        yield from rglob_follow_symlinks(real_path, pattern)

        logger.info("Built queue object.")
        tqdm_queue = Queue()
        aspect_ratio_bucket_indices_queue = Queue()
        logger.info("Build file list..")
        all_image_files = list(
            rglob_follow_symlinks(Path(self.instance_data_root), "*.[jJpP][pPnN][gG]")
        )
        self._length = len(all_image_files)
        logger.info("Split file list into shards.")
        files_split = np.array_split(all_image_files, 8)
        existing_files_set = set().union(*self.aspect_ratio_bucket_indices.values())
        workers = []
        logger.info("Process lists...")
        for files in files_split:
            p = Process(
                target=self._bucket_worker,
                args=(
                    tqdm_queue,
                    files,
                    aspect_ratio_bucket_indices_queue,
                    existing_files_set,
                ),
            )
            p.start()
            workers.append(p)

        # Update progress bar and gather results in main process
        with tqdm(total=len(all_image_files)) as pbar:
            while any(p.is_alive() for p in workers):
                while not tqdm_queue.empty():
                    pbar.update(tqdm_queue.get())
                while not aspect_ratio_bucket_indices_queue.empty():
                    aspect_ratio_bucket_indices.update(
                        aspect_ratio_bucket_indices_queue.get()
                    )

        # Gather any remaining results
        while not aspect_ratio_bucket_indices_queue.empty():
            aspect_ratio_bucket_indices.update(aspect_ratio_bucket_indices_queue.get())
        logger.info("Join processes and finish up.")
        for p in workers:
            p.join()

        # Step 3: Updating the Cache
        new_file_paths = [
            str(file)
            for file in all_image_files
            if str(file) not in self.instance_images_path
        ]

        # Update the instance_images_path to include the new images
        self.instance_images_path += new_file_paths

        # Update the total number of instance images
        self.num_instance_images = len(self.instance_images_path)

        # Save updated aspect_ratio_bucket_indices to the cache file
        with cache_file.open("w") as f:
            logger.info("Writing updated cache file to disk")
            json.dump(aspect_ratio_bucket_indices, f)

        logger.info("Completed aspect bucket update.")

        return aspect_ratio_bucket_indices

    def assign_to_buckets(self):
        cache_file = self.instance_data_root / "aspect_ratio_bucket_indices.json"
        return self.compute_aspect_ratio_bucket_indices(cache_file)

    def __len__(self):
        return self._length

    def get_all_captions(self):
        captions = []

        def rglob_follow_symlinks(path: Path, pattern: str):
            for p in path.glob(pattern):
                yield p
            for p in path.iterdir():
                if p.is_dir() and not p.is_symlink():
                    yield from rglob_follow_symlinks(p, pattern)
                elif p.is_symlink():
                    real_path = Path(os.readlink(p))
                    if real_path.is_dir():
                        yield from rglob_follow_symlinks(real_path, pattern)

        all_image_files = list(
            rglob_follow_symlinks(Path(self.instance_data_root), "*.[jJpP][pPnN][gG]")
        )

        for image_path in all_image_files:
            caption = self._prepare_instance_prompt(str(image_path))
            captions.append(caption)

        return captions

    def _prepare_instance_prompt(self, image_path):
        instance_prompt = self.instance_prompt
        if self.use_captions:
            instance_prompt = Path(image_path).stem
            # Remove underscores and swap with spaces:
            instance_prompt = instance_prompt.replace("_", " ")
            instance_prompt = instance_prompt.split("upscaled by")[0]
            instance_prompt = instance_prompt.split("upscaled beta")[0]
            if self.prepend_instance_prompt:
                instance_prompt = self.instance_prompt + " " + instance_prompt
        if self.print_names:
            logger.debug(f"Prompt: {instance_prompt}")
        return instance_prompt

    def caption_loop_interval_bump(self):
        self.caption_loop_count += 1
        if self.caption_loop_count > 100:
            self.caption_loop_count = 0

    def __getitem__(self, image_path):
        if self.debug_dataset_loader:
            logger.debug(f"Running __getitem__ for {image_path} inside Dataloader.")
        if not StateTracker.status_training():
            if self.debug_dataset_loader:
                logger.warning(f"Skipping getitem because we are not yet training.")
            return None
        example = {"instance_images_path": image_path}
        if self.print_names and self.debug_dataset_loader:
            logger.debug(f"Open image: {image_path}")
        instance_image = Image.open(image_path)
        # Apply EXIF transformations.
        instance_image = exif_transpose(instance_image)
        if self.caption_strategy == "filename":
            instance_prompt = self._prepare_instance_prompt(image_path)
        elif self.caption_strategy == "textfile":
            caption_file = Path(image_path).with_suffix(".txt")
            if not caption_file.exists():
                raise FileNotFoundError(f"Caption file {caption_file} not found.")
            with caption_file.open("r") as f:
                instance_prompt = f.read()
        else:
            raise ValueError(f"Unsupported caption strategy: {self.caption_strategy}")
        if not instance_image.mode == "RGB" and StateTracker.status_training():
            instance_image = instance_image.convert("RGB")
        if StateTracker.status_training():
            logger.debug(f"Resizing sample to {self.size}")
            example["instance_images"] = self._resize_for_condition_image(
                instance_image, self.size
            )
        else:
            example["instance_images"] = instance_image
        if not self.use_original_images and StateTracker.status_training():
            example["instance_images"] = self.image_transforms(instance_image)
        example["instance_prompt_ids"] = None
        if StateTracker.status_training():
            if self.caption_dropout_interval > 0:
                if self.caption_loop_count % self.caption_dropout_interval == 0:
                    if self.debug_dataset_loader:
                        logger.debug(
                            f"Caption dropout, removing caption: {instance_prompt}"
                        )
                    instance_prompt = ""
                self.caption_loop_interval_bump()
            if not self.use_precomputed_token_ids:
                example["instance_prompt_ids"] = self.tokenizer(
                    instance_prompt,
                    truncation=True,
                    padding="max_length",
                    max_length=self.tokenizer.model_max_length,
                    return_tensors="pt",
                ).input_ids
        example["instance_prompt_text"] = instance_prompt
        if self.debug_dataset_loader:
            logger.debug(
                f"Returning from __getitem__ for {image_path} inside Dataloader."
            )
        return example

    def _resize_for_condition_image(self, input_image: Image, resolution: int):
        input_image = input_image.convert("RGB")
        W, H = input_image.size
        aspect_ratio = round(W / H, 2)
        msg = f"Inspecting image of aspect {aspect_ratio} and size {W}x{H} to "
        if W < H:
            W = resolution
            H = int(resolution / aspect_ratio)  # Calculate the new height
        elif H < W:
            H = resolution
            W = int(resolution * aspect_ratio)  # Calculate the new width
        if W == H:
            W = resolution
            H = resolution
        msg = f"{msg} {W}x{H}."
        if self.debug_dataset_loader:
            logger.debug(msg)
        img = input_image.resize((W, H), resample=Image.BICUBIC)
        return img