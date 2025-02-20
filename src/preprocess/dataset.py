"""
 - Reference. 2016, Investigating RNN-based speech enhancement methods for noise-robust Text-to-Speech
 
 ITU-T P.56 method [26] to calculate active speech levels using the code provided in [13]. 
 The clean waveforms were added to noise after they had been normalised and silence segments 
 longer than 200 ms had been trimmed off from the beginning and end of each sentence.
"""
import os
import tqdm
import librosa
import numpy as np
import tensorflow as tf
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from .feature_extractor import FeatureExtractor
from src.utils import (
    get_tf_feature_real_imag_pair,
    get_tf_feature_mag_phase_pair,
    get_tf_feature_sample_pair,
    read_audio,
    segment_audio,
    encode_normalize,
)

# import logging
# import math
# import multiprocessing
# from sklearn.preprocessing import StandardScaler


class DatasetVoiceBank:
    def __init__(self, clean_filenames, noisy_filenames, name, args, debug=False):
        self.clean_filenames = clean_filenames
        self.noisy_filenames = noisy_filenames
        self.model_name = name
        self.args = args
        self.debug = debug

    def _sample_noisy_filename(self):
        return np.random.choice(self.noisy_filenames)

    def _remove_silent_frames(self, audio, index_indices=None, name=None):
        trimed_audio = []

        if index_indices is None:
            indices = librosa.effects.split(
                audio, hop_length=self.args.hop_length, top_db=self.args.top_db
            )  # average mse in each frame < -20 dB
        else:
            indices = index_indices

        audio_remove_slience = np.zeros_like(audio)
        for index in indices:
            audio_remove_slience[index[0] : index[1]] = audio[index[0] : index[1]]

        for index in indices:
            trimed_audio.extend(audio[index[0] : index[1]])

        return indices, np.array(trimed_audio)

    def _phase_aware_scaling(self, clean_spectral_magnitude, clean_phase, noise_phase):
        assert clean_phase.shape == noise_phase.shape, "Shapes must match."
        return clean_spectral_magnitude * np.cos(clean_phase - noise_phase)

    def audio_process(self, filename):
        clean_filename, noisy_filename = filename
        assert (
            clean_filename.split("/")[-1] == noisy_filename.split("/")[-1]
        ), "filename must match."

        name = (
            clean_filename.split("/")[-1].split(".")[0]
            + "_"
            + clean_filename.split("/")[-1].split(".")[1]
        )

        clean_audio, sr = read_audio(clean_filename, self.args.sample_rate)
        noisy_audio, sr = read_audio(noisy_filename, self.args.sample_rate)

        if not self.args.segment_normalization:
            clean_audio = encode_normalize(clean_audio, self.args.normalize)
            noisy_audio = encode_normalize(noisy_audio, self.args.normalize)

        # # remove silent frame from clean audio
        #     noisy_index, noisy_audio = self._remove_silent_frames(noisy_audio, None, noisy_filename)
        #     noisy_index, clean_audio = self._remove_silent_frames(clean_audio, noisy_index, clean_filename)

        # sample random fixed-sized snippets of audio
        clean_audio = segment_audio(
            clean_audio, self.args.sample_rate, self.args.segment
        )
        noisy_audio = segment_audio(
            noisy_audio, self.args.sample_rate, self.args.segment
        )

        if self.args.segment_normalization:
            clean_audio = encode_normalize(clean_audio, self.args.normalize)
            noisy_audio = encode_normalize(noisy_audio, self.args.normalize)

        if self.args.fft:
            # extract stft features from noisy audio
            noisy_input_fe = FeatureExtractor(
                noisy_audio,
                windowLength=self.args.win_length,
                hop_length=self.args.hop_length,
                sample_rate=self.args.sample_rate,
            )
            noisy_spectrogram = noisy_input_fe.get_stft_spectrogram(self.args.center)

            # Or get the phase angle (in radians)
            # noisy_stft_magnitude, noisy_stft_phase = librosa.magphase(noisy_stft_features)
            noisy_phase = np.angle(noisy_spectrogram)

            # get the magnitude of the spectral
            noisy_magnitude = np.abs(noisy_spectrogram)

            # extract stft features from clean audio
            clean_audio_fe = FeatureExtractor(
                clean_audio,
                windowLength=self.args.win_length,
                hop_length=self.args.hop_length,
                sample_rate=self.args.sample_rate,
            )
            clean_spectrogram = clean_audio_fe.get_stft_spectrogram(self.args.center)
            # clean_spectrogram = cleanAudioFE.get_mel_spectrogram()

            # get the clean phase
            clean_phase = np.angle(clean_spectrogram)

            # get the clean spectral magnitude
            clean_magnitude = np.abs(clean_spectrogram)
            # clean_magnitude = 2 * clean_magnitude / np.sum(scipy.signal.hamming(self.args.win_length, sym=False))

            noisy_real, noisy_imag = np.real(noisy_spectrogram), np.imag(
                noisy_spectrogram
            )
            clean_real, clean_imag = np.real(clean_spectrogram), np.imag(
                clean_spectrogram
            )

            # called phase aware scaling
            # clean_magnitude = self._phase_aware_scaling(clean_magnitude, clean_phase, noisy_phase)
            # scaler = StandardScaler(copy=False, with_mean=True, with_std=True)
            # noisy_magnitude = scaler.fit_transform(noisy_magnitude)
            # clean_magnitude = scaler.transform(clean_magnitude)

            return name, (
                noisy_magnitude,
                clean_magnitude,
                noisy_phase,
                clean_phase,
                noisy_real,
                clean_real,
                noisy_imag,
                clean_imag,
            )
        else:
            return name, (noisy_audio, clean_audio)

    def create_tf_record(self, *, prefix, parallel=False):
        root = self.args.save_path
        folder = f"{root}/records_seg_{str(self.args.segment).replace('.', '-')}_train_{int(self.args.split*100)}_norm_{self.args.normalize}_segNorm_{self.args.segment_normalization}_fft_{self.args.fft}_topdB_{self.args.top_db}"
        if self.debug:
            folder = f"{folder}_debug"

        if not os.path.exists(folder):
            os.mkdir(folder)

        if self.debug:
            file_name_list = [
                (clean_filename, noisy_filename)
                for clean_filename, noisy_filename in zip(
                    self.clean_filenames[:100], self.noisy_filenames[:100]
                )
            ]
        else:
            file_name_list = [
                (clean_filename, noisy_filename)
                for clean_filename, noisy_filename in zip(
                    self.clean_filenames, self.noisy_filenames
                )
            ]

        # If file list for preprocess didn't divide, then it will stack in memory(RAM)
        start = 0
        step = 100
        end = step

        print(f"Total {prefix} file number: {len(file_name_list)}")

        for istep in tqdm.tqdm(range(len(file_name_list) // step), ncols=120):
            if istep == len(file_name_list) // step - 1:
                submitted_file_name_list = file_name_list[start:]
            else:
                submitted_file_name_list = file_name_list[start:end]

            if parallel:
                print(f"CPU ", os.cpu_count() - 3 if os.cpu_count() > 4 else 1, "...")
                out = []
                pendings = []
                with ProcessPoolExecutor(
                    os.cpu_count() - 3 if os.cpu_count() > 4 else 1
                ) as pool:
                    for file_name in submitted_file_name_list:
                        pendings.append(pool.submit(self.audio_process, file_name))

                    for pending in tqdm.tqdm(pendings):
                        out.append(pending.result())
                # out = p.map(self.parallel_audio_processing, clean_filenames_sublist)
            else:
                out = [
                    self.audio_process(file_names)
                    for file_names in submitted_file_name_list
                ]

            for name, data in out:
                if self.args.fft:
                    # noisy_stft_magnitude = data[0]
                    # clean_stft_magnitude = data[1]
                    # noisy_stft_phase = data[2]
                    # clean_stft_phase = data[3]
                    noisy_stft_real = data[4]
                    clean_stft_real = data[5]
                    noisy_stft_imag = data[6]
                    clean_stft_imag = data[7]
                    if self.debug:
                        print("  Getting from preprocess")
                        print(
                            "[DEBUG]: ",
                            noisy_stft_real.shape,
                            noisy_stft_imag.shape,
                            clean_stft_real.shape,
                            clean_stft_imag.shape,
                        )
                        print(
                            "[DEBUG]: ",
                            noisy_stft_real.dtype,
                            noisy_stft_imag.dtype,
                            clean_stft_real.dtype,
                            clean_stft_imag.dtype,
                        )
                        print("---")

                    # segment, ch, frame, freqeuncy
                    new_axes = np.arange(len(clean_stft_imag.shape))
                    new_axes[-2:] = new_axes[-1], new_axes[-2]

                    noisy_stft_real = np.transpose(
                        noisy_stft_real, axes=new_axes
                    )
                    clean_stft_real = np.transpose(
                        clean_stft_real, axes=new_axes
                    )
                    noisy_stft_imag = np.transpose(
                        noisy_stft_imag, axes=new_axes
                    )
                    clean_stft_imag = np.transpose(
                        clean_stft_imag, axes=new_axes
                    )  
                    
                    if self.debug:
                        print(" Segmentation")
                        print(
                            "[DEBUG]: ",
                            noisy_stft_real.shape,
                            noisy_stft_imag.shape,
                            clean_stft_real.shape,
                            clean_stft_imag.shape,
                        )
                        print(
                            "[DEBUG]: ",
                            noisy_stft_real.dtype,
                            noisy_stft_imag.dtype,
                            clean_stft_real.dtype,
                            clean_stft_imag.dtype,
                        )
                        print("---")

                    for idata, (
                        noisy_real,
                        clean_real,
                        noisy_imag,
                        clean_imag,
                    ) in enumerate(
                        zip(
                            noisy_stft_real,
                            clean_stft_real,
                            noisy_stft_imag,
                            clean_stft_imag,
                        )
                    ):
                        # 1, ch, frame, freqeuncy
                        noisy_real = np.expand_dims(noisy_real, axis=0)  
                        clean_real = np.expand_dims(clean_real, axis=0)
                        noisy_imag = np.expand_dims(noisy_imag, axis=0)
                        clean_imag = np.expand_dims(clean_imag, axis=0)

                        if self.debug:
                            print("  Write Down to tfrecord")
                            print(
                                "[DEBUG]: ",
                                noisy_real.shape,
                                noisy_imag.shape,
                                clean_real.shape,
                                clean_imag.shape,
                            )
                            print(
                                "[DEBUG]: ",
                                noisy_real.dtype,
                                noisy_imag.dtype,
                                clean_real.dtype,
                                clean_imag.dtype,
                            )
                            print("---")

                        example = get_tf_feature_real_imag_pair(
                            noisy_real, clean_real, noisy_imag, clean_imag
                        )
    
                        tfrecord_filename = (
                            f"{folder}/{prefix}_{name}_{idata}.tfrecords"
                        )
                        if os.path.isfile(
                            tfrecord_filename
                        ):  # [TODO] Why at first time, it goes here?
                            print(f"Skipping {tfrecord_filename}")
                            continue
                        else:
                            writer = tf.io.TFRecordWriter(tfrecord_filename)
                            writer.write(example.SerializeToString())
                            writer.close()
                else:
                    noisy_audio = data[0]
                    clean_audio = data[1]

                    if self.debug:
                        print("[DEBUG]: ", noisy_audio.shape, clean_audio.shape)

                    for idata, (noise_segment, clean_segment) in enumerate(
                        zip(noisy_audio, clean_audio)
                    ):
                        if self.debug:
                            print("  Write Down to tfrecord")
                            print("[DEBUG]: ", noise_segment.shape, clean_segment.shape)
                            print("---")

                        example = get_tf_feature_sample_pair(
                            noise_segment, clean_segment
                        )
                        tfrecord_filename = (
                            f"{folder}/{prefix}_{name}_{idata}.tfrecords"
                        )
                        if os.path.isfile(tfrecord_filename):
                            print(f"Skipping {tfrecord_filename}")
                            continue
                        else:
                            writer = tf.io.TFRecordWriter(tfrecord_filename)
                            writer.write(example.SerializeToString())
                            writer.close()

            del out  # resolve memory leak
            start += step
            end += step
