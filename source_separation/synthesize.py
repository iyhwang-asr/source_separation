from pathlib import Path

import numpy as np
import fire
import torch
import librosa
import os
import glob

from joblib import Parallel, delayed
from pytorch_sound.data.dataset import SpeechDataLoader
from torch.utils.data import Dataset, DataLoader

import source_separation
from tqdm import tqdm
from pytorch_sound import settings
from pytorch_sound.utils.commons import get_loadable_checkpoint
from pytorch_sound.models import build_model
from pytorch_sound.utils.sound import lowpass, inv_preemphasis, preemphasis
from pytorch_sound.models.sound import PreEmphasis
from pytorch_sound.data.meta import voice_bank


def __load_model(model_name: str, pretrained_path: str) -> torch.nn.Module:
    print('Load model ...')
    model = build_model(model_name).cuda()
    chk = torch.load(pretrained_path)['model']
    model.load_state_dict(get_loadable_checkpoint(chk))
    model.eval()
    return model


def run(audio_file: str, out_path: str, model_name: str, pretrained_path: str, lowpass_freq: int = 0,
        sample_rate: int = 22050):
    print('Loading audio file...')
    wav, sr = librosa.load(audio_file, sr=sample_rate)
    wav = preemphasis(wav)

    if wav.dtype != np.float32:
        wav = wav.astype(np.float32)

    # load model
    model = __load_model(model_name, pretrained_path)

    # make tensor wav
    wav = torch.FloatTensor(wav).unsqueeze(0).cuda()

    # inference
    print('Inference ...')
    with torch.no_grad():
        out_wav = model(wav)
        out_wav = out_wav[0].cpu().numpy()

    if lowpass_freq:
        out_wav = lowpass(out_wav, frequency=lowpass_freq)

    # save wav
    librosa.output.write_wav(out_path, inv_preemphasis(out_wav).clip(-1., 1.), sample_rate)

    print('Finish !')


def validate(meta_dir: str, out_dir: str, model_name: str, pretrained_path: str, batch_size: int = 64,
             num_workers: int = 16,):
    preemp = PreEmphasis().cuda()

    # load model
    model = __load_model(model_name, pretrained_path)

    # load validation data loader
    _, valid_loader = voice_bank.get_datasets(
        meta_dir, batch_size=batch_size, num_workers=num_workers, fix_len=None, audio_mask=True
    )

    # mkdir
    os.makedirs(out_dir, exist_ok=True)

    # loop all
    print('Process Validation Dataset ...')
    noise_all = []
    clean_all = []
    results = []
    for noise, clean, *others in tqdm(valid_loader):
        noise = noise.cuda()
        noise = preemp(noise.unsqueeze(1)).squeeze(1)
        with torch.no_grad():
            clean_hat = model(noise)
        noise_all.append(noise)
        clean_all.append(clean)
        results.append(clean_hat)

    # write all
    print('Write all result into {} ...'.format(out_dir))
    for idx, (batch_clean_hat, batch_noise, batch_clean) in tqdm(enumerate(zip(results, noise_all, clean_all))):
        for in_idx, (clean_hat, noise, clean) in enumerate(zip(batch_clean_hat, batch_noise, batch_clean)):
            noise_out_path = os.path.join(out_dir, '{}_noise.wav'.format(idx * batch_size + in_idx))
            pred_out_path = os.path.join(out_dir, '{}_pred.wav'.format(idx * batch_size + in_idx))
            clean_out_path = os.path.join(out_dir, '{}_clean.wav'.format(idx * batch_size + in_idx))

            librosa.output.write_wav(clean_out_path, clean.cpu().numpy(), settings.SAMPLE_RATE)
            librosa.output.write_wav(noise_out_path,
                                     inv_preemphasis(noise.cpu().numpy()), settings.SAMPLE_RATE)
            librosa.output.write_wav(pred_out_path,
                                     inv_preemphasis(clean_hat.cpu().numpy()).clip(-1., 1.), settings.SAMPLE_RATE)

    print('Finish !')


def test_worker(out_wav, file_path, in_dir, out_dir, sample_rate, l):
    # make output path
    sub_dir = os.path.dirname(file_path).replace(in_dir, '')
    file_out_dir = os.path.join(out_dir, sub_dir)
    os.makedirs(file_out_dir, exist_ok=True)
    out_file_path = os.path.join(file_out_dir, os.path.basename(file_path))
    out_wav = out_wav[:l]
    out_wav = inv_preemphasis(out_wav.squeeze())
    librosa.output.write_wav(out_file_path, out_wav, sample_rate)


class WaveDataset(Dataset):

    def __init__(self, wav_list, sample_rate):
        self.wav_list = wav_list
        self.sample_rate = sample_rate

    def __getitem__(self, idx):
        wav = librosa.load(self.wav_list[idx], sr=self.sample_rate)[0]
        return [preemphasis(wav), np.array([len(wav)])]

    def __len__(self):
        return len(self.wav_list)


def test_dir(in_dir: str, out_dir: str, model_name: str, pretrained_path: str, sample_rate: int = 22050,
             num_workers: int = 4, batch_size: int = 64):
    # lookup files
    print('Lookup wave files ...')
    wav_list = list(map(str, Path(in_dir).glob('**/*.wav')))

    # load models
    model = __load_model(model_name, pretrained_path)

    # dataloader
    dataset = WaveDataset(wav_list, sample_rate)
    data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=False,
                             num_workers=num_workers, collate_fn=SpeechDataLoader.pad_collate_fn)

    # mkdir
    os.makedirs(out_dir, exist_ok=True)

    with Parallel(n_jobs=num_workers) as parallel:

        for dp, batch_idx in tqdm(zip(data_loader, range(0, len(wav_list), batch_size))):
            batch_wav = dp[0].cuda()
            lens = dp[1].numpy()

            with torch.no_grad():
                batch_clean_hat = model(batch_wav)

            batch_clean_hat = batch_clean_hat.cpu().numpy()

            zipping_list = zip(batch_clean_hat, wav_list[batch_idx:batch_idx + batch_size], lens)

            parallel(
                delayed(test_worker)
                (out_wav, file_path, in_dir, out_dir, sample_rate, int(l))
                for out_wav, file_path, l in zipping_list
            )

    print('Finish !')


if __name__ == '__main__':
    fire.Fire({
        'separate': run,
        'validate': validate,
        'test_dir': test_dir
    })
