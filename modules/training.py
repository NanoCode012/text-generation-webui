import json
import math
import random
import sys
import threading
import time
import traceback
from pathlib import Path

import gradio as gr
import torch
import transformers

import shutil
from datetime import datetime

from datasets import Dataset, load_dataset
from peft import (
    LoraConfig,
    get_peft_model,
    prepare_model_for_int8_training,
    set_peft_model_state_dict
)

from modules import shared, ui, utils
from modules.evaluate import (
    calculate_perplexity,
    generate_markdown_table,
    save_past_evaluations
)
from modules.logging_colors import logger

# This mapping is from a very recent commit, not yet released.
# If not available, default to a backup map for some common model types.
try:
    from peft.utils.other import \
        TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING as \
        model_to_lora_modules
    from transformers.models.auto.modeling_auto import (
        MODEL_FOR_CAUSAL_LM_MAPPING_NAMES
    )
    MODEL_CLASSES = {v: k for k, v in MODEL_FOR_CAUSAL_LM_MAPPING_NAMES}
except:
    standard_modules = ["q_proj", "v_proj"]
    model_to_lora_modules = {"llama": standard_modules, "opt": standard_modules, "gptj": standard_modules, "gpt_neox": ["query_key_value"], "rw": ["query_key_value"]}
    MODEL_CLASSES = {
        "LlamaForCausalLM": "llama",
        "OPTForCausalLM": "opt",
        "GPTJForCausalLM": "gptj",
        "GPTNeoXForCausalLM": "gpt_neox",
        "RWForCausalLM": "rw"

    }

train_log = {}
train_template = {}

WANT_INTERRUPT = False
PARAMETERS = ["lora_name", "always_override", "save_steps", "micro_batch_size", "batch_size", "epochs", "learning_rate", "lr_scheduler_type", "lora_rank", "lora_alpha", "lora_dropout", "cutoff_len", "dataset", "eval_dataset", "format", "eval_steps", "raw_text_file", "overlap_len", "newline_favor_len", "higher_rank_limit", "warmup_steps", "optimizer", "hard_cut_string", "train_only_after", "stop_at_loss"]


def create_train_interface():
    with gr.Tab('Train LoRA', elem_id='lora-train-tab'):
        gr.Markdown("Confused? [[Click here for a guide]](https://github.com/oobabooga/text-generation-webui/blob/main/docs/Training-LoRAs.md)")

        with gr.Row():
            lora_name = gr.Textbox(label='Name', info='The name of your new LoRA file')
            always_override = gr.Checkbox(label='Override Existing Files', value=False, info='If the name given is the same as an existing file, checking this will replace that file. Leaving unchecked will load that file and continue from it (must use the same rank value as the original had).')
            save_steps = gr.Number(label='Save every n steps', value=0, info='If above 0, a checkpoint of the LoRA will be saved every time this many steps pass.')

        with gr.Row():
            copy_from = gr.Dropdown(label='Copy parameters from', value='None', choices=utils.get_available_loras())
            ui.create_refresh_button(copy_from, lambda: None, lambda: {'choices': utils.get_available_loras()}, 'refresh-button')

        with gr.Row():
            # TODO: Implement multi-device support.
            micro_batch_size = gr.Slider(label='Micro Batch Size', value=4, minimum=1, maximum=128, step=1, info='Per-device batch size (NOTE: multiple devices not yet implemented). Increasing this will increase VRAM usage.')
            batch_size = gr.Slider(label='Batch Size', value=128, minimum=0, maximum=1024, step=4, info='Global batch size. The two batch sizes together determine gradient accumulation (gradientAccum = batch / microBatch). Higher gradient accum values lead to better quality training.')

        with gr.Row():
            epochs = gr.Number(label='Epochs', value=3, info='Number of times every entry in the dataset should be fed into training. So 1 means feed each item in once, 5 means feed it in five times, etc.')
            learning_rate = gr.Textbox(label='Learning Rate', value='3e-4', info='Learning rate, in scientific notation. 3e-4 is a good starting base point. 1e-2 is extremely high, 1e-6 is extremely low.')
            lr_scheduler_type = gr.Dropdown(label='LR Scheduler', value='linear', choices=['linear', 'constant', 'constant_with_warmup', 'cosine', 'cosine_with_restarts', 'polynomial', 'inverse_sqrt'], info='Learning rate scheduler - defines how the learning rate changes over time. "Constant" means never change, "linear" means to go in a straight line from the learning rate down to 0, cosine follows a curve, etc.')

        # TODO: What is the actual maximum rank? Likely distinct per model. This might be better to somehow be on a log scale.
        lora_rank = gr.Slider(label='LoRA Rank', value=32, minimum=0, maximum=1024, step=4, info='LoRA Rank, or dimension count. Higher values produce a larger file with better control over the model\'s content. Smaller values produce a smaller file with less overall control. Small values like 4 or 8 are great for stylistic guidance, higher values like 128 or 256 are good for teaching content upgrades, extremely high values (1024+) are difficult to train but may improve fine-detail learning for large datasets. Higher ranks also require higher VRAM.')
        lora_alpha = gr.Slider(label='LoRA Alpha', value=64, minimum=0, maximum=2048, step=4, info='LoRA Alpha. This divided by the rank becomes the scaling of the LoRA. Higher means stronger. A good standard value is twice your Rank.')

        cutoff_len = gr.Slider(label='Cutoff Length', minimum=0, maximum=2048, value=256, step=32, info='Cutoff length for text input. Essentially, how long of a line of text to feed in at a time. Higher values require drastically more VRAM.')

        with gr.Tab(label='Formatted Dataset'):
            with gr.Row():
                dataset = gr.Dropdown(choices=utils.get_datasets('training/datasets', 'json'), value='None', label='Dataset', info='The dataset file to use for training.')
                ui.create_refresh_button(dataset, lambda: None, lambda: {'choices': utils.get_datasets('training/datasets', 'json')}, 'refresh-button')
                eval_dataset = gr.Dropdown(choices=utils.get_datasets('training/datasets', 'json'), value='None', label='Evaluation Dataset', info='The (optional) dataset file used to evaluate the model after training.')
                ui.create_refresh_button(eval_dataset, lambda: None, lambda: {'choices': utils.get_datasets('training/datasets', 'json')}, 'refresh-button')
                format = gr.Dropdown(choices=utils.get_datasets('training/formats', 'json'), value='None', label='Data Format', info='The format file used to decide how to format the dataset input.')
                ui.create_refresh_button(format, lambda: None, lambda: {'choices': utils.get_datasets('training/formats', 'json')}, 'refresh-button')

            eval_steps = gr.Number(label='Evaluate every n steps', value=100, info='If an evaluation dataset is given, test it every time this many steps pass.')

        with gr.Tab(label="Raw text file"):
            with gr.Row():
                raw_text_file = gr.Dropdown(choices=utils.get_datasets('training/datasets', 'txt'), value='None', label='Text file', info='The raw text file to use for training.')
                ui.create_refresh_button(raw_text_file, lambda: None, lambda: {'choices': utils.get_datasets('training/datasets', 'txt')}, 'refresh-button')
                hard_cut_string = gr.Textbox(label='Hard Cut String', value='\\n\\n\\n', info='String that indicates a hard cut between text parts. Helps prevent unwanted overlap.')

            with gr.Row():
                overlap_len = gr.Slider(label='Overlap Length', minimum=0, maximum=512, value=128, step=16, info='Overlap length - ie how many tokens from the prior chunk of text to include into the next chunk. (The chunks themselves will be of a size determined by Cutoff Length below). Setting overlap to exactly half the cutoff length may be ideal.')
                newline_favor_len = gr.Slider(label='Prefer Newline Cut Length', minimum=0, maximum=512, value=128, step=16, info='Length (in characters, not tokens) of the maximum distance to shift an overlap cut by to ensure chunks cut at newlines. If too low, cuts may occur in the middle of lines.')

        with gr.Accordion(label='Advanced Options', open=False):
            lora_dropout = gr.Slider(label='LoRA Dropout', minimum=0.0, maximum=1.0, step=0.025, value=0.05, info='Percentage probability for dropout of LoRA layers. This can help reduce overfitting. Most users should leave at default.')
            warmup_steps = gr.Number(label='Warmup Steps', value=100, info='For this many steps at the start, the learning rate will be lower than normal. This helps the trainer prepare the model and precompute statistics to improve the quality of training after the start.')
            optimizer = gr.Dropdown(label='Optimizer', value='adamw_torch', choices=['adamw_hf', 'adamw_torch', 'adamw_torch_fused', 'adamw_torch_xla', 'adamw_apex_fused', 'adafactor', 'adamw_bnb_8bit', 'adamw_anyprecision', 'sgd', 'adagrad'], info='Different optimizer implementation options, for advanced users. Effects of different options are not well documented yet.')
            train_only_after = gr.Textbox(label='Train Only After', value='', info='Only consider text *after* this string in any given chunk for training. For Alpaca datasets, use "### Response:" to only train the response and ignore the input.')
            stop_at_loss = gr.Slider(label='Stop at loss', minimum=0.0, maximum=3.0, step=0.1, value=0.00, info='The process will automatically stop once the desired loss value is reached. (reasonable numbers are 1.5-1.8)')

            with gr.Row():
                higher_rank_limit = gr.Checkbox(label='Enable higher ranks', value=False, info='If checked, changes Rank/Alpha slider above to go much higher. This will not work without a datacenter-class GPU.')

        with gr.Row():
            start_button = gr.Button("Start LoRA Training")
            stop_button = gr.Button("Interrupt")

        output = gr.Markdown(value="Ready")

    with gr.Tab('Perplexity evaluation', elem_id='evaluate-tab'):
        with gr.Row():
            with gr.Column():
                models = gr.Dropdown(utils.get_available_models(), label='Models', multiselect=True)
                evaluate_text_file = gr.Dropdown(choices=['wikitext', 'ptb', 'ptb_new'] + utils.get_datasets('training/datasets', 'txt')[1:], value='wikitext', label='Input dataset', info='The raw text file on which the model will be evaluated. The first options are automatically downloaded: wikitext, ptb, and ptb_new. The next options are your local text files under training/datasets.')
                with gr.Row():
                    stride_length = gr.Slider(label='Stride', minimum=1, maximum=2048, value=512, step=1, info='Used to make the evaluation faster at the cost of accuracy. 1 = slowest but most accurate. 512 is a common value.')
                    max_length = gr.Slider(label='max_length', minimum=0, maximum=8096, value=0, step=1, info='The context for each evaluation. If set to 0, the maximum context length for the model will be used.')

                with gr.Row():
                    start_current_evaluation = gr.Button("Evaluate loaded model")
                    start_evaluation = gr.Button("Evaluate selected models")
                    stop_evaluation = gr.Button("Interrupt")

            with gr.Column():
                evaluation_log = gr.Markdown(value='')

        evaluation_table = gr.Dataframe(value=generate_markdown_table(), interactive=True)
        with gr.Row():
            save_comments = gr.Button('Save comments', elem_classes="small-button")
            refresh_table = gr.Button('Refresh the table', elem_classes="small-button")

    # Training events
    all_params = [lora_name, always_override, save_steps, micro_batch_size, batch_size, epochs, learning_rate, lr_scheduler_type, lora_rank, lora_alpha, lora_dropout, cutoff_len, dataset, eval_dataset, format, eval_steps, raw_text_file, overlap_len, newline_favor_len, higher_rank_limit, warmup_steps, optimizer, hard_cut_string, train_only_after, stop_at_loss]
    copy_from.change(do_copy_params, [copy_from] + all_params, all_params)
    start_button.click(do_train, all_params, output)
    stop_button.click(do_interrupt, None, None, queue=False)
    higher_rank_limit.change(change_rank_limit, [higher_rank_limit], [lora_rank, lora_alpha])

    # Evaluation events. For some reason, the interrupt event
    # doesn't work with the .then() syntax, so I write them one
    # by one in this ugly but functional way.
    ev = start_evaluation.click(calculate_perplexity, [models, evaluate_text_file, stride_length, max_length], evaluation_log, show_progress=False)
    start_evaluation.click(generate_markdown_table, None, evaluation_table, show_progress=False)

    tmp = gr.State('')
    start_current_evaluation.click(lambda: ['current model'], None, tmp)
    ev_cur = start_current_evaluation.click(calculate_perplexity, [tmp, evaluate_text_file, stride_length, max_length], evaluation_log, show_progress=False)
    start_current_evaluation.click(generate_markdown_table, None, evaluation_table, show_progress=False)

    stop_evaluation.click(None, None, None, cancels=[ev, ev_cur], queue=False)
    refresh_table.click(generate_markdown_table, None, evaluation_table, show_progress=True)
    save_comments.click(
        save_past_evaluations, evaluation_table, None).then(
        lambda: "Comments saved.", None, evaluation_log, show_progress=False)


def do_interrupt():
    global WANT_INTERRUPT
    WANT_INTERRUPT = True


def do_copy_params(lora_name: str, *args):
    f_name = f"{shared.args.lora_dir}/{clean_path(None, lora_name)}/training_parameters.json"
    if Path(f_name).is_file():
        with open(f_name, 'r', encoding='utf-8') as format_file:
            params: dict[str, str] = json.load(format_file)
    else:
        params = {}

    result = list()
    for i in range(0, len(PARAMETERS)):
        key = PARAMETERS[i]
        if key in params:
            result.append(params[key])
        else:
            result.append(args[i])

    return result


def change_rank_limit(use_higher_ranks: bool):
    mult = 2 if use_higher_ranks else 1
    return {"maximum": 1024 * mult, "__type__": "update"}, {"maximum": 2048 * mult, "__type__": "update"}


def clean_path(base_path: str, path: str):
    """Strips unusual symbols and forcibly builds a path as relative to the intended directory."""
    # TODO: Probably could do with a security audit to guarantee there's no ways this can be bypassed to target an unwanted path.
    # Or swap it to a strict whitelist of [a-zA-Z_0-9]
    path = path.replace('\\', '/').replace('..', '_')
    if base_path is None:
        return path

    return f'{Path(base_path).absolute()}/{path}'


def backup_adapter(input_folder):
    # Get the creation date of the file adapter_model.bin
    try:
        adapter_file = Path(f"{input_folder}/adapter_model.bin")
        if adapter_file.is_file():

            logger.info("Backing up existing LoRA adapter...")
            creation_date = datetime.fromtimestamp(adapter_file.stat().st_ctime)
            creation_date_str = creation_date.strftime("Backup-%Y-%m-%d")

            # Create the new subfolder
            subfolder_path = Path(f"{input_folder}/{creation_date_str}") 
            subfolder_path.mkdir(parents=True, exist_ok=True)

            # Check if the file already exists in the subfolder
            backup_adapter_file = Path(f"{input_folder}/{creation_date_str}/adapter_model.bin")
            if backup_adapter_file.is_file():
                print(" - Backup already exists. Skipping backup process.")
                return

            # Copy existing files to the new subfolder
            existing_files = Path(input_folder).iterdir()
            for file in existing_files:
                if file.is_file():
                    shutil.copy2(file, subfolder_path)
    except Exception as e:
        print("An error occurred in backup_adapter:", str(e))


def do_train(lora_name: str, always_override: bool, save_steps: int, micro_batch_size: int, batch_size: int, epochs: int, learning_rate: str, lr_scheduler_type: str, lora_rank: int, lora_alpha: int, lora_dropout: float, cutoff_len: int, dataset: str, eval_dataset: str, format: str, eval_steps: int, raw_text_file: str, overlap_len: int, newline_favor_len: int, higher_rank_limit: bool, warmup_steps: int, optimizer: str, hard_cut_string: str, train_only_after: str, stop_at_loss: float):

    if shared.args.monkey_patch:
        from monkeypatch.peft_tuners_lora_monkey_patch import (
            replace_peft_model_with_gptq_lora_model
        )
        replace_peft_model_with_gptq_lora_model()

    global WANT_INTERRUPT
    WANT_INTERRUPT = False

    # == Input validation / processing ==
    yield "Prepping..."
    lora_file_path = clean_path(None, lora_name)
    if lora_file_path.strip() == '':
        yield "Missing or invalid LoRA file name input."
        return

    lora_file_path = f"{shared.args.lora_dir}/{lora_file_path}"
    actual_lr = float(learning_rate)
    model_type = type(shared.model).__name__

    if model_type in MODEL_CLASSES:
        model_id = MODEL_CLASSES[model_type]
    else:
        model_id = "llama"
        if model_type == "PeftModelForCausalLM":
            if len(shared.args.lora_names) > 0:
                yield "You are trying to train a LoRA while you already have another LoRA loaded. This will work, but may have unexpected effects. *(Will continue anyway in 5 seconds, press `Interrupt` to stop.)*"
                logger.warning("Training LoRA over top of another LoRA. May have unexpected effects.")
            else:
                yield "Model ID not matched due to LoRA loading. Consider reloading base model. *(Will continue anyway in 5 seconds, press `Interrupt` to stop.)*"
                logger.warning("Model ID not matched due to LoRA loading. Consider reloading base model.")
        else:
            yield "LoRA training has only currently been validated for LLaMA, OPT, GPT-J, and GPT-NeoX models. Unexpected errors may follow. *(Will continue anyway in 5 seconds, press `Interrupt` to stop.)*"
            logger.warning(f"LoRA training has only currently been validated for LLaMA, OPT, GPT-J, and GPT-NeoX models. (Found model type: {model_type})")

        time.sleep(5)

    if shared.args.wbits > 0 and not shared.args.monkey_patch:
        yield "LoRA training with GPTQ models requires loading with `--monkey-patch`"
        return

    elif not (shared.args.load_in_8bit or shared.args.load_in_4bit) and shared.args.wbits <= 0:
        yield "It is highly recommended you use `--load-in-8bit` for LoRA training. *(Will continue anyway in 2 seconds, press `Interrupt` to stop.)*"
        logger.warning("It is highly recommended you use `--load-in-8bit` for LoRA training.")
        time.sleep(2)  # Give it a moment for the message to show in UI before continuing

    if cutoff_len <= 0 or micro_batch_size <= 0 or batch_size <= 0 or actual_lr <= 0 or lora_rank <= 0 or lora_alpha <= 0:
        yield "Cannot input zeroes."
        return

    gradient_accumulation_steps = batch_size // micro_batch_size
    shared.tokenizer.pad_token_id = 0
    shared.tokenizer.padding_side = "left"

    def encode(text, add_bos_token):
        result = shared.tokenizer.encode(text, truncation=True, max_length=cutoff_len)
        if not add_bos_token and result[0] == shared.tokenizer.bos_token_id:
            result = result[1:]
        return result

    def tokenize(prompt):

        if train_only_after == '' or train_only_after not in prompt:
            input_ids = encode(prompt, True)
            input_ids = [shared.tokenizer.pad_token_id] * (cutoff_len - len(input_ids)) + input_ids
            labels = [1] * len(input_ids)

        else:
            ind = prompt.index(train_only_after) + len(train_only_after)
            before_tokens = encode(prompt[:ind], True)
            after_tokens = encode(prompt[ind:], False)

            full_length = len(after_tokens) + len(before_tokens)
            if full_length > cutoff_len:
                after_tokens = after_tokens[:cutoff_len - len(before_tokens)]
            else:
                before_tokens = [shared.tokenizer.pad_token_id] * (cutoff_len - full_length) + before_tokens

            input_ids = before_tokens + after_tokens
            labels = [-100] * len(before_tokens) + [1] * len(after_tokens)

        input_ids = torch.tensor(input_ids)
        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": input_ids.ne(shared.tokenizer.pad_token_id),
        }

    train_template.clear()

    # == Prep the dataset, format, etc ==
    if raw_text_file not in ['None', '']:
        logger.info("Loading raw text file dataset...")

        train_template["template_type"] = "raw_text"

        with open(clean_path('training/datasets', f'{raw_text_file}.txt'), 'r', encoding='utf-8') as file:
            raw_text = file.read().replace('\r', '')

        cut_string = hard_cut_string.replace('\\n', '\n')
        out_tokens = []
        for text_part in raw_text.split(cut_string):
            if text_part.strip() == '':
                continue

            tokens = shared.tokenizer.encode(text_part)
            step = cutoff_len - overlap_len
            if step <= 0:
                yield f"Error: overlap_len ({overlap_len}) cannot be greater than or equal to cutoff_len ({cutoff_len})"
                return

            tokens = list(split_chunks(tokens, step))
            for i in range(1, len(tokens)):
                tokens[i] = tokens[i - 1][-overlap_len:] + tokens[i]

            out_tokens.extend(tokens)
            del tokens

        del raw_text  # Note: could be a gig for a large dataset, so delete redundant data as we go to be safe on RAM
        text_chunks = [shared.tokenizer.decode(x) for x in out_tokens]
        del out_tokens
        if newline_favor_len > 0:
            text_chunks = [cut_chunk_for_newline(x, newline_favor_len) for x in text_chunks]

        train_data = Dataset.from_list([tokenize(x) for x in text_chunks])
        del text_chunks
        eval_data = None
    else:
        if dataset in ['None', '']:
            yield "**Missing dataset choice input, cannot continue.**"
            return

        if format in ['None', '']:
            yield "**Missing format choice input, cannot continue.**"
            return

        train_template["template_type"] = "dataset"

        with open(clean_path('training/formats', f'{format}.json'), 'r', encoding='utf-8-sig') as formatFile:
            format_data: dict[str, str] = json.load(formatFile)

        # == store training prompt ==
        for _, value in format_data.items():
            prompt_key = f"template_{len(train_template)}"
            train_template[prompt_key] = value

        def generate_prompt(data_point: dict[str, str]):
            for options, data in format_data.items():
                if set(options.split(',')) == set(x[0] for x in data_point.items() if (x[1] is not None and len(x[1].strip()) > 0)):
                    for key, val in data_point.items():
                        if val is not None:
                            data = data.replace(f'%{key}%', val)
                    return data
            raise RuntimeError(f'Data-point "{data_point}" has no keyset match within format "{list(format_data.keys())}"')

        def generate_and_tokenize_prompt(data_point):
            prompt = generate_prompt(data_point)
            return tokenize(prompt)

        logger.info("Loading JSON datasets...")
        data = load_dataset("json", data_files=clean_path('training/datasets', f'{dataset}.json'))
        train_data = data['train'].map(generate_and_tokenize_prompt, new_fingerprint='%030x' % random.randrange(16**30))

        if eval_dataset == 'None':
            eval_data = None
        else:
            eval_data = load_dataset("json", data_files=clean_path('training/datasets', f'{eval_dataset}.json'))
            eval_data = eval_data['train'].map(generate_and_tokenize_prompt, new_fingerprint='%030x' % random.randrange(16**30))

    # == Start prepping the model itself ==
    if not hasattr(shared.model, 'lm_head') or hasattr(shared.model.lm_head, 'weight'):
        logger.info("Getting model ready...")
        prepare_model_for_int8_training(shared.model)

    logger.info("Prepping for training...")
    config = LoraConfig(
        r=lora_rank,
        lora_alpha=lora_alpha,
        target_modules=model_to_lora_modules[model_id],
        lora_dropout=lora_dropout,
        bias="none",
        task_type="CAUSAL_LM"
    )

    # == Backup the existing adapter ==
    if not always_override:
        backup_adapter(lora_file_path)

    try:
        logger.info("Creating LoRA model...")
        lora_model = get_peft_model(shared.model, config)
        if not always_override and Path(f"{lora_file_path}/adapter_model.bin").is_file():
            logger.info("Loading existing LoRA data...")
            state_dict_peft = torch.load(f"{lora_file_path}/adapter_model.bin")
            set_peft_model_state_dict(lora_model, state_dict_peft)
    except:
        yield traceback.format_exc()
        return

    if shared.args.monkey_patch:
        for n, m in lora_model.named_modules():
            if '4bit' in str(type(m)):
                if m.is_v1_model:
                    m.zeros = m.zeros.half()

                m.scales = m.scales.half()

    class Tracked():
        def __init__(self):
            self.current_steps = 0
            self.max_steps = 0
            self.did_save = False

    tracked = Tracked()
    actual_save_steps = math.ceil(save_steps / gradient_accumulation_steps)

    class Callbacks(transformers.TrainerCallback):
        def on_step_begin(self, args: transformers.TrainingArguments, state: transformers.TrainerState, control: transformers.TrainerControl, **kwargs):
            tracked.current_steps = state.global_step * gradient_accumulation_steps
            tracked.max_steps = state.max_steps * gradient_accumulation_steps
            if WANT_INTERRUPT:
                control.should_epoch_stop = True
                control.should_training_stop = True
            elif state.global_step > 0 and actual_save_steps > 0 and state.global_step % actual_save_steps == 0:
                lora_model.save_pretrained(f"{lora_file_path}/checkpoint-{tracked.current_steps}/")
                # Save log
                with open(f"{lora_file_path}/checkpoint-{tracked.current_steps}/training_log.json", 'w', encoding='utf-8') as file:
                    json.dump(train_log, file, indent=2)
                # == Save training prompt ==
                with open(f"{lora_file_path}/checkpoint-{tracked.current_steps}/training_prompt.json", 'w', encoding='utf-8') as file:
                    json.dump(train_template, file, indent=2)

        def on_substep_end(self, args: transformers.TrainingArguments, state: transformers.TrainerState, control: transformers.TrainerControl, **kwargs):
            tracked.current_steps += 1
            if WANT_INTERRUPT:
                control.should_epoch_stop = True
                control.should_training_stop = True

        def on_log(self, args: transformers.TrainingArguments, state: transformers.TrainerState, control: transformers.TrainerControl, logs, **kwargs):
            train_log.update(logs)
            train_log.update({"current_steps": tracked.current_steps})
            if WANT_INTERRUPT:
                print("\033[1;31;1mInterrupted by user\033[0;37;0m")

            print(f"\033[1;30;40mStep: {tracked.current_steps} \033[0;37;0m", end='')
            if 'loss' in logs:
                loss = float(logs['loss'])
                if loss <= stop_at_loss:
                    control.should_epoch_stop = True
                    control.should_training_stop = True
                    print(f"\033[1;31;1mStop Loss {stop_at_loss} reached.\033[0;37;0m")

    trainer = transformers.Trainer(
        model=lora_model,
        train_dataset=train_data,
        eval_dataset=eval_data,
        args=transformers.TrainingArguments(
            per_device_train_batch_size=micro_batch_size,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_steps=math.ceil(warmup_steps / gradient_accumulation_steps),
            num_train_epochs=epochs,
            learning_rate=actual_lr,
            fp16=False if shared.args.cpu else True,
            optim=optimizer,
            logging_steps=2 if stop_at_loss > 0 else 5,
            evaluation_strategy="steps" if eval_data is not None else "no",
            eval_steps=math.ceil(eval_steps / gradient_accumulation_steps) if eval_data is not None else None,
            save_strategy="steps" if eval_data is not None else "no",
            output_dir=lora_file_path,
            lr_scheduler_type=lr_scheduler_type,
            load_best_model_at_end=eval_data is not None,
            # TODO: Enable multi-device support
            ddp_find_unused_parameters=None,
            no_cuda=shared.args.cpu
        ),
        data_collator=transformers.DataCollatorForLanguageModeling(shared.tokenizer, mlm=False),
        callbacks=list([Callbacks()])
    )

    lora_model.config.use_cache = False

    if torch.__version__ >= "2" and sys.platform != "win32":
        lora_model = torch.compile(lora_model)

    # == Save parameters for reuse ==
    with open(f"{lora_file_path}/training_parameters.json", 'w', encoding='utf-8') as file:
        vars = locals()
        json.dump({x: vars[x] for x in PARAMETERS}, file, indent=2)

    # == Save training prompt ==
    with open(f"{lora_file_path}/training_prompt.json", 'w', encoding='utf-8') as file:
        json.dump(train_template, file, indent=2)

    # == Main run and monitor loop ==
    logger.info("Starting training...")
    yield "Starting..."

    train_log.update({"base_model_name": shared.model_name})
    train_log.update({"base_model_class": shared.model.__class__.__name__})
    train_log.update({"base_loaded_in_4bit": getattr(lora_model, "is_loaded_in_4bit", False)})
    train_log.update({"base_loaded_in_8bit": getattr(lora_model, "is_loaded_in_8bit", False)})

    if stop_at_loss > 0:
        print(f"Monitoring loss \033[1;31;1m(Auto-Stop at: {stop_at_loss})\033[0;37;0m")

    if WANT_INTERRUPT:
        yield "Interrupted before start."
        return

    def threaded_run():
        trainer.train()
        # Note: save in the thread in case the gradio thread breaks (eg browser closed)
        lora_model.save_pretrained(lora_file_path)
        logger.info("LoRA training run is completed and saved.")
        # Save log
        with open(f"{lora_file_path}/training_log.json", 'w', encoding='utf-8') as file:
            json.dump(train_log, file, indent=2)

    thread = threading.Thread(target=threaded_run)
    thread.start()
    last_step = 0
    start_time = time.perf_counter()

    while thread.is_alive():
        time.sleep(0.5)
        if WANT_INTERRUPT:
            yield "Interrupting, please wait... *(Run will stop after the current training step completes.)*"

        elif tracked.current_steps != last_step:
            last_step = tracked.current_steps
            time_elapsed = time.perf_counter() - start_time
            if time_elapsed <= 0:
                timer_info = ""
                total_time_estimate = 999
            else:
                its = tracked.current_steps / time_elapsed
                if its > 1:
                    timer_info = f"`{its:.2f}` it/s"
                else:
                    timer_info = f"`{1.0/its:.2f}` s/it"

                total_time_estimate = (1.0 / its) * (tracked.max_steps)

            yield f"Running... **{tracked.current_steps}** / **{tracked.max_steps}** ... {timer_info}, {format_time(time_elapsed)} / {format_time(total_time_estimate)} ... {format_time(total_time_estimate - time_elapsed)} remaining"

    # Saving in the train thread might fail if an error occurs, so save here if so.
    if not tracked.did_save:
        logger.info("Training complete, saving...")
        lora_model.save_pretrained(lora_file_path)

    if WANT_INTERRUPT:
        logger.info("Training interrupted.")
        yield f"Interrupted. Incomplete LoRA saved to `{lora_file_path}`"
    else:
        logger.info("Training complete!")
        yield f"Done! LoRA saved to `{lora_file_path}`"


def split_chunks(arr, step):
    for i in range(0, len(arr), step):
        yield arr[i:i + step]


def cut_chunk_for_newline(chunk: str, max_length: int):
    if '\n' not in chunk:
        return chunk

    first_newline = chunk.index('\n')
    if first_newline < max_length:
        chunk = chunk[first_newline + 1:]

    if '\n' not in chunk:
        return chunk

    last_newline = chunk.rindex('\n')
    if len(chunk) - last_newline < max_length:
        chunk = chunk[:last_newline]

    return chunk


def format_time(seconds: float):
    if seconds < 120:
        return f"`{seconds:.0f}` seconds"

    minutes = seconds / 60
    if minutes < 120:
        return f"`{minutes:.0f}` minutes"

    hours = minutes / 60
    return f"`{hours:.0f}` hours"
