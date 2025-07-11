from custom import args, device, get_checkpoint_path
import os
import torch
import torch.nn.functional as F
import numpy as np
from datasets import load_dataset
import config as CFG
from transformers import LlamaConfig, LlamaModel, AutoTokenizer
from modules import CBL
from utils import eos_pooling
import evaluate
import time


class ClassificationDataset(torch.utils.data.Dataset):
    def __init__(self, encoded_text):
        self.encoded_text = encoded_text


    def __getitem__(self, idx):
        t = {key: torch.tensor(values[idx]) for key, values in self.encoded_text.items()}
        return t

    def __len__(self):
        return len(self.encoded_text['input_ids'])


def build_loaders(encoded_text, mode):
    dataset = ClassificationDataset(encoded_text)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size, num_workers=args.num_workers,
                                             shuffle=True if mode == "train" else False)
    return dataloader



if __name__ == "__main__":
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    print("loading data...")
    test_dataset = load_dataset(args.dataset, split='test')
    print("test data len: ", len(test_dataset))
    print("tokenizing...")
    config = LlamaConfig.from_pretrained(args.model_id)
    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    tokenizer.pad_token = tokenizer.eos_token

    if args.dataset == 'ag_news':
        def replace_bad_string(example):
            example["text"] = example["text"].replace("#36;", "")
            example["text"] = example["text"].replace("#39;", "'")
            return example
        test_dataset = test_dataset.map(replace_bad_string)

    encoded_test_dataset = test_dataset.map(
        lambda e: tokenizer(e[CFG.example_name[args.dataset]], padding=True, truncation=True,
                            max_length=args.max_length), batched=True, batch_size=len(test_dataset))
    encoded_test_dataset = encoded_test_dataset.remove_columns([CFG.example_name[args.dataset]])
    if args.dataset == 'SetFit/sst2':
        encoded_test_dataset = encoded_test_dataset.remove_columns(['label_text'])
    if args.dataset == 'dbpedia_14':
        encoded_test_dataset = encoded_test_dataset.remove_columns(['title'])
    encoded_test_dataset = encoded_test_dataset[:len(encoded_test_dataset)]

    concept_set = CFG.concepts_from_labels[args.dataset]
    print("concept len: ", len(concept_set))

    print("creating loader...")
    test_loader = build_loaders(encoded_test_dataset, mode="test")

    print("preparing backbone")
    peft_path = get_checkpoint_path("from_pretained_llama3_lora_cbm/" + args.dataset.replace('/', '_') + "/llama3", args.epoch_index)
    cbl_path = get_checkpoint_path("from_pretained_llama3_lora_cbm/" + args.dataset.replace('/', '_') + "/cbl", args.epoch_index)
    preLM = LlamaModel.from_pretrained(args.model_id, torch_dtype=torch.bfloat16).to(device)
    preLM.load_adapter(peft_path)
    preLM.eval()
    cbl = CBL(config, len(concept_set), tokenizer).to(device)
    cbl.load_state_dict(torch.load(cbl_path, map_location=device))
    cbl.eval()

    print("eval concepts...")
    metric = evaluate.load("accuracy")
    concept_predictions = []
    for batch in test_loader:
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.no_grad():
            features = preLM(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]).last_hidden_state
            concepts, _, _ = cbl(features.float())
        concept_predictions.append(eos_pooling(concepts, batch["attention_mask"]))
    concept_predictions = torch.cat(concept_predictions, dim=0).detach().cpu()
    pred = np.argmax(concept_predictions.numpy(), axis=-1)
    metric.add_batch(predictions=pred, references=encoded_test_dataset["label"])
    print(metric.compute())
