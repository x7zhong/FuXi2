import argparse
import os
import time 
import numpy as np
import xarray as xr
import pandas as pd
import onnxruntime as ort
from data_util import *

parser = argparse.ArgumentParser()
parser.add_argument('--model_dir', type=str, required=True, help="FuXi onnx model dir")
parser.add_argument('--data_dir', type=str, required=True, help="Input data dir")
parser.add_argument('--save_dir', type=str, default="", help="Where to save the results")
parser.add_argument('--input', type=str, default="", help="The input data file, store in netcdf format")
parser.add_argument('--device', type=str, default="cuda", help="The device to run FuXi model")
parser.add_argument('--device_id', type=int, default=0, help="Which gpu to use")
parser.add_argument('--version', type=str, default="c79")
parser.add_argument('--total_step', type=int, default=1)
parser.add_argument('--use_interp', action="store_true")
args = parser.parse_args()


model_urls = {
    "short": os.path.join(args.model_dir, f"short.onnx"),
    "interp": os.path.join(args.model_dir, f"interp.onnx"),
}


def save_with_progress(ds, save_name, dtype=np.float32):
    from dask.diagnostics import ProgressBar

    if 'time' in ds.dims:
        ds = ds.assign_coords(time=ds.time.astype(np.datetime64))

    ds = ds.astype(dtype)

    if save_name.endswith("nc"):
        obj = ds.to_netcdf(save_name, compute=False)
    elif save_name.endswith("zarr"):
        obj = ds.to_zarr(save_name, compute=False)

    with ProgressBar():
        obj.compute()


def save_like(output, input, lead_time):
    save_dir = args.save_dir

    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        init_time = pd.to_datetime(input.time.values[-1])
        lead_times = np.arange(lead_time-output.shape[1], lead_time) + 1

        ds = xr.DataArray(
            data=output,
            dims=['time', 'lead_time', 'channel', 'lat', 'lon'],
            coords=dict(
                time=[init_time],
                lead_time=lead_times,
                channel=input.channel,
                lat=input.lat.values,
                lon=input.lon.values,
            )
        ).astype(np.float32)
        print_dataarray(ds)
        save_name = os.path.join(save_dir, f'{lead_time:03d}.nc')
        save_with_progress(ds, save_name)


def time_encoding(init_time, total_step, freq=6):
    init_time = np.array([init_time])
    tembs = []
    for i in range(total_step):
        hours = np.array([pd.Timedelta(hours=t*freq) for t in [i-1, i, i+1]])
        times = init_time[:, None] + hours[None]
        times = [pd.Period(t, 'H') for t in times.reshape(-1)]
        times = [(p.day_of_year/366, p.hour/24) for p in times]
        temb = np.array(times, dtype=np.float32)
        temb = np.concatenate([np.sin(temb), np.cos(temb)], axis=-1)
        temb = temb.reshape(1, -1)
        tembs.append(temb)
    return np.stack(tembs)



def load_model(model_name, device):
    ort.set_default_logger_severity(3)
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena=False
    options.enable_mem_pattern = False
    options.enable_mem_reuse = False
    # Increase the number for faster inference and more memory consumption

    # cuda_provider_options = {"arena_extend_strategy": "kSameAsRequested", "do_copy_in_default_stream": False, "cudnn_conv_use_max_workspace": "1"}
    # cpu_provider_options = {"arena_extend_strategy": "kSameAsRequested", "do_copy_in_default_stream": False}
    # execution_providers = [("CUDAExecutionProvider", cuda_provider_options), ("CPUExecutionProvider", cpu_provider_options)]
    # session = ort.InferenceSession(model_name,  providers=execution_providers)
    # return session

    if device == "cuda":
        providers = ['CUDAExecutionProvider']
        provider_options = [{'device_id': args.device_id}]
    elif device == "cpu":
        providers=['CPUExecutionProvider']
        options.intra_op_num_threads = 24
    else:
        raise ValueError("device must be cpu or cuda!")

    session = ort.InferenceSession(model_name, 
        sess_options=options, 
        providers=providers,
        provider_options=provider_options
    )
    return session


def run_inference(models, input, total_step):
    lat = input.lat.values 
    hist_time = pd.to_datetime(input.time.values[-2])
    init_time = pd.to_datetime(input.time.values[-1])
    time_str = init_time.strftime("%Y%m%d%H")

    assert init_time - hist_time == pd.Timedelta(hours=6)
    assert lat[0] == 90 and lat[-1] == -90
    batch = input.values[None]
    print(f'Inference initial time: {time_str} ...')

    start = time.perf_counter()
    for step in range(total_step):
        lead_time = (step + 1) * 6
        valid_time = init_time + pd.Timedelta(hours=step * 6)
        model = models["short"]
        input_names = [x.name for x in model.get_inputs()]
        inputs = {'input': batch}        

        if "step" in input_names:
            inputs['step'] = np.array([step], dtype=np.float32)

        if "hour" in input_names:
            hour = valid_time.hour/24 
            inputs['hour'] = np.array([hour], dtype=np.float32)

        if "doy" in input_names:
            doy = min(365, valid_time.day_of_year)/365
            inputs['doy'] = np.array([doy], dtype=np.float32)
        
        t0 = time.perf_counter()
        new_input, = model.run(None, inputs)
        output = new_input[:, -1:]

        if args.use_interp:
            inputs['input'] = new_input
            output, = models["interp"].run(None, inputs)

        run_time = time.perf_counter() - t0
        print(f"lead_time: {lead_time:03d} h, run_time: {run_time:.3f} secs")
        save_like(output, input, lead_time)
        batch = new_input

    total_time = time.perf_counter() - start
    print(f'Inference done take {total_time:.2f}')



def load_input():
    assert os.path.exists(args.data_dir)
    file_name = os.path.join(os.path.dirname(args.data_dir), "input.nc")
    if os.path.exists(file_name):
        input = xr.open_dataarray(file_name)
    else:
        input = make_sample(args.data_dir, version=args.version)
        input.to_netcdf(file_name)
    print_dataarray(input)
    return input

if __name__ == "__main__":
    input = load_input()
        
    models = {}
    for k, file_name in model_urls.items():
        if os.path.exists(file_name):
            print(f'Load FuXi {k} ...')       
            start = time.perf_counter()
            model = load_model(file_name, args.device)            
            models[k] = model
            print(f'Load FuXi {k} take {time.perf_counter() - start:.2f} sec')

    run_inference(models, input, args.total_step)
