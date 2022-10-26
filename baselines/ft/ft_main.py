from copy import deepcopy
from typing import Any, Dict, List, Tuple

import torch
import numpy as np
from transformers import AutoModelForCausalLM, AutoTokenizer

from experiments.causal_trace import find_token_range, simple_make_inputs, corrupted_forward_pass
from util import nethook

from .ft_hparams import FTHyperParams


def apply_ft_to_model(
    args,
    model: AutoModelForCausalLM,
    tok: AutoTokenizer,
    requests: List[Dict],
    hparams: FTHyperParams,
    copy=False,
    return_orig_weights=False,
    **kwargs: Any,
) -> Tuple[AutoModelForCausalLM, Dict[str, Any]]:
    """
    Returns a model with the desired changes.
    :param copy: If true, will preserve the original model while creating a new one to edit.
        Note that you are responsible for deallocating the new model's memory to avoid leaks.
    :return: (1) the updated model, (2) the weights that changed
    """
    assert len(requests) == 1
    weights_copy = {}
    if copy:
        model = deepcopy(model)

    # get more ft arguments; subj idx for embedding finetuning, number of times to repeat sample for optimization under noised subjects, and 
    if min(hparams.layers) == -1 and hparams.FT_subj_embeds:
        assert len(requests) == 1
        subject = requests[0]['subject']
        subject_idx = tok.encode(subject)
        embeds_subj_idx = np.array(subject_idx)
    else:
        embeds_subj_idx = None
    num_noise_samples = kwargs.pop('num_noise_samples', None)
    prior_prob = kwargs.pop('prior_prob', None)

    # get hidden representations from corrupted+uncorrupted forward passes to use as targets for weight editing
    if args.weight_based_tracing:
        prompt = requests[0]['full_prompt']
        subject = requests[0]['subject']
        e_range = find_token_range(tok, substring=subject, prompt_str=prompt)
        last_subj_idx = e_range[1]
        gen_batch = simple_make_inputs(tok, prompts=[prompt] * (num_noise_samples))
        gen_batch['output_hidden_states'] = True
        import pdb; pdb.set_trace()
        _, _, corrupted_hidden_states = corrupted_forward_pass(mt.model, None, gen_batch, tokens_to_mix=e_range, noise=hparams.editing_noise, output_hidden_states=True)
        uncorrupted_hidden_states = mt.model(**gen_batch)
        # splice uncorrupted hidden_states into corrupted_hidden_states where they are restored
        hidden_state_supervision = corrupted_hidden_states
        hidden_state_supervision[:,last_subj_idx,:] = uncorrupted_hidden_states[:,last_subj_idx,:]
    else:
        hidden_state_supervision = None

    deltas = execute_ft(args, model, tok, requests, hparams, 
                        embedding_token_idx=embeds_subj_idx, 
                        repeat_input=num_noise_samples, 
                        prior_prob=prior_prob,
                        hidden_state_supervision=hidden_state_supervision)

    with torch.no_grad():
        for w_name, upd_matrix in deltas.items():
            w = nethook.get_parameter(model, w_name)
            if return_orig_weights and w_name not in weights_copy:
                weights_copy[w_name] = w.detach().clone()

            is_embeddings = 'wte' in w_name or 'embedding' in w_name
            if is_embeddings and hparams.FT_subj_embeds:
                print("only updating embeddings for tokens: ", embeds_subj_idx)
                w[embeds_subj_idx,:] += upd_matrix[embeds_subj_idx,:]
            else:
                w[...] += upd_matrix

    print(f"New weights successfully inserted into {list(deltas.keys())}")

    return model, weights_copy


def execute_ft(
    args,
    model: AutoModelForCausalLM,
    tok: AutoTokenizer,
    requests: List[Dict],
    hparams: FTHyperParams,
    embedding_token_idx = None,
    repeat_input = 1,
    prior_prob = 0,
    hidden_state_supervision = None,
    **kwargs: Any,
) -> Dict[str, Tuple[torch.Tensor]]:
    """
    Executes the FT update algorithm for the specified update at the specified layer
    Invariant: model at beginning of function == model at end of function
    """
    patience_counter = 0

    # Update target and print info
    requests = deepcopy(requests)
    for request in requests:
        if request["target_new"]["str"][0] != " ":
            # Space required for correct tokenization
            request["target_new"]["str"] = " " + request["target_new"]["str"]
        print(
            f"Executing FT algo for: "
            f"[{request['prompt'].format(request['subject'])}] -> [{request['target_new']['str']}]"
        )
        if args.fact_erasure:
            print(f"prior prob is: {prior_prob:.4f}")

    # Retrieve weights that user desires to change
    weights = {
        n: p
        for n, p in model.named_parameters()
        for layer in hparams.layers
        if hparams.rewrite_module_tmp.format(layer) in n
    }
    # add embeddings
    # will zero out grads for non subject token idx as needed later
    if min(hparams.layers) == -1:
        weights.update({n: p for n,p in model.named_parameters() if 'embedding' in n or 'wte' in n})
        assert len(weights) == 1, "more than one token embeddding matrix?"
    # Save old weights for future restoration
    weights_copy = {k: v.detach().clone() for k, v in weights.items()}
    print(f"Weights to be updated: {list(weights.keys())}")

    # Define inputs
    texts = [r["prompt"].format(r["subject"]) for r in requests]
    targets = [r["target_new"]["str"] for r in requests] # if args.fact_erasure, these are set as target_true
    
    # Configure optimizer / gradients
    opt = torch.optim.Adam(
        [v for _, v in weights.items()],
        lr=hparams.lr,
        weight_decay=hparams.weight_decay,
    )
    for name, w in model.named_parameters():
        w.requires_grad = name in weights

    # Update loop: intervene at layers simultaneously
    loss_meter = AverageMeter()
    for it in range(hparams.num_steps):
        loss_meter.reset()

        for txt, tgt in zip(
            chunks(texts, hparams.batch_size), chunks(targets, hparams.batch_size)
        ):
            inputs = tok(txt, return_tensors="pt", padding=True).to("cuda")
            target_ids = tok(tgt, return_tensors="pt", padding=True)["input_ids"].to("cuda")
            inputs = {k: v.repeat(repeat_input,1) for k,v in inputs.items()}
            target_ids = target_ids.repeat(repeat_input,1)
            last_token_inds = inputs["attention_mask"].sum(dim=1) - 1
            loss_mask = target_ids != tok.unk_token_id
            if args.weight_based_tracing:
                inputs['return_hidden_states'] = True

            opt.zero_grad()
            bs = inputs["input_ids"].shape[0]
            outputs = model(**inputs)
            last_token_logits = outputs.logits[torch.arange(bs), last_token_inds]

            # compute loss based on objective
            if not args.weight_based_tracing:
                probs = torch.nn.functional.log_softmax(last_token_logits, dim=-1)
                loss = -(torch.gather(probs, 1, target_ids) * loss_mask).sum(
                    1
                ) / loss_mask.sum(1)
            if args.fact_erasure:
                pred_prob = -torch.exp(loss)
                loss = torch.abs(pred_prob - prior_prob) 
            if args.weight_based_tracing:
                hidden_states = outputs.hidden_states
                pass
                
            loss = loss.mean()
            loss_meter.update(loss.item(), n=bs)

            if loss.item() >= 1e-2:
                loss.backward()
                # zero out grad for embeds
                if embedding_token_idx is not None:
                    embeddings = [v for v in weights.values()][0]
                    n_embeds = embeddings.size(0)
                    non_subj_embeds = np.setdiff1d(np.arange(n_embeds), embedding_token_idx)
                    embeddings.grad[non_subj_embeds,:] = 0
                opt.step()

            if type(hparams.norm_constraint) is float:
                eps = hparams.norm_constraint
                with torch.no_grad():
                    for k, v in weights.items():
                        v[...] = torch.clamp(
                            v, min=weights_copy[k] - eps, max=weights_copy[k] + eps
                        )
        print(f"Total loss at epoch {it}: {loss_meter.avg:.4f} ", f" (pred prob: {pred_prob:.4f})" if args.fact_erasure else "")

        if loss_meter.avg < 1e-2:
            patience_counter += 1
            if patience_counter >= 5:
                break
        else:
            patience_counter = 0

    deltas = {k: (weights[k] - weights_copy[k]).detach() for k in weights}

    # Restore state of original model
    with torch.no_grad():
        for k, v in weights.items():
            v[...] = weights_copy[k]

    print(f"Deltas successfully computed for {list(weights.keys())}")

    return deltas


def chunks(arr, n):
    """Yield successive n-sized chunks from arr."""
    chunk = []
    for a in arr:
        chunk.append(a)
        if len(chunk) == n:
            yield chunk
            chunk = []
    if len(chunk) > 0:
        yield chunk

class AverageMeter:
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count
