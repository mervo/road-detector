import numpy as np
import pandas as pd
from PIL import Image
from tqdm import tqdm
import os
import re
from glob import glob
import yaml
import gdal

from shapely.geometry import LineString, MultiLineString
from skimage.morphology import skeletonize
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.ops import linemerge

import warnings
from torch.serialization import SourceChangeWarning
warnings.simplefilter('ignore', SourceChangeWarning)

DIST_FACTOR = 3 # how much to scale the pixelwise distance transform

def ImageId_to_iid(ImageId):
    assert ImageId.startswith('AOI_')
    num = int(ImageId.split('img')[-1])
    micro_aoi = ImageId[6].lower()
    return '%s%04d' % (micro_aoi, num)
def iid_AOI(iid):
    return {'v': 'AOI_2_Vegas', 'p': 'AOI_3_Paris', 's': 'AOI_4_Shanghai', 'k': 'AOI_5_Khartoum'}[iid[0]]
def iid_num(iid):
    return int(iid[1:])
def iid_ImageId(iid):
    return '%s_img%d' % (iid_AOI(iid), iid_num(iid))

STANDARD_PRODUCTS = { 'PAN': 'tif', 'MUL': 'tif', 'MUL-PanSharpen': 'tif', 'RGB-PanSharpen': 'tif' }

def iid_path(iid, product, ext=None):
    if ext is None:
        if product in STANDARD_PRODUCTS:
            ext = STANDARD_PRODUCTS[product]
        elif product.endswith('_png') or product.endswith('_jpg'):
            ext = product[-3:]
    assert ext is not None
    if product in STANDARD_PRODUCTS:
        root = 'spacenet'
    else:
        root = 'computed'
    fmt = dict(root=root, ImageId=iid, product=product, ext=ext)
    return '{root}/{product}/{product}_{ImageId}.{ext}'.format(**fmt)

def dict_metadata_by_fold(val_fold):
    trainval_files = os.listdir('spacenet/Train/RGB')
    trainval_metadata = pd.DataFrame(trainval_files)
    trainval_metadata = trainval_metadata.set_index(0).sort_index()
    metadata_by_fold = {}
    if val_fold=='w':
        metadata_by_fold['val'] = trainval_metadata.iloc[:0]
    else:
        i = 'xyz'.index(val_fold)
        metadata_by_fold['val'] = trainval_metadata.iloc[i::3]
    metadata_by_fold['train'] = trainval_metadata.drop(metadata_by_fold['val'].index)
    #metadata_by_fold['test'] = all_metadata().query('source!="Train"')
    return metadata_by_fold

def as_MultiLineString(shape):
    if isinstance(shape, LineString):
        return MultiLineString([shape])
    return shape

def is_road(pixel):
    if abs(pixel[0]- pixel[1]) < 60 and  abs(pixel[0]- pixel[2])<60:
        return False
    else:       
        return True

def build_dist_aniso(force_rebuild=False, tqdm=tqdm):
    if not os.path.exists('spacenet/Train/Labels'):
        return
    iids = os.listdir('spacenet/Train/Labels')
    #for (faoi,iid),g in tqdm(summaryData.groupby(['full_aoi', 'ImageId']).groups.items(), desc="build_dist_aniso"):
    for iid in tqdm(iids, desc="build_dist_aniso"):
        #dist_path = 'computed/%s/dist_aniso_png/dist_aniso_png_%s.png' % (faoi, iid)
        dist_path = iid_path(iid, "dist_aniso_png")
        if force_rebuild or not os.path.exists(dist_path):
            from PIL import Image, ImageDraw
            data = Image.open('spacenet/Train/Labels/'+iid)
            WIDTH, HEIGHT = data.size
            pixels = data.load()
            im = Image.new('L', (WIDTH,HEIGHT), 255) 
            draw = ImageDraw.Draw(im)
            for y in range (HEIGHT):
                for x in range (WIDTH):
                    if is_road(pixels[x,y]):
                        draw.point([x,y], fill=0)
            n_pixels = np.sum(np.asarray(im) != 255)
            dt = ndimage.distance_transform_edt(im)
            if n_pixels == 0:
                dt[:,:] = np.inf
            im = Image.fromarray(np.round(DIST_FACTOR*dt).clip(0,255).astype(np.uint8))
            os.makedirs(os.path.dirname(dist_path), exist_ok=True)
            im.save(dist_path)

   

def build_rgb_aniso(force_rebuild=False):
    for iid in tqdm(all_iid(), desc="build_rgb_aniso"):
        out_path = iid_path(iid, 'rgb_aniso_jpg')
        if force_rebuild or not os.path.exists(out_path):
            tiff_path = iid_path(iid, 'RGB-PanSharpen')
            rgb = preprocess_rgb_image(tiff_path)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            rgb.save(out_path, quality=95, subsampling=0)

def build_mask_aniso(force_rebuild=False):
    for iid in tqdm(all_iid(), desc="build_mask_aniso"):
        out_path = iid_path(iid, 'mask_aniso_png')
        if force_rebuild or not os.path.exists(out_path):
            tiff_path = iid_path(iid, 'RGB-PanSharpen')
            raw = load_tiff(tiff_path)
            mask = Image.fromarray((raw.max(axis=2) != 0) * np.uint8(255))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            mask.save(out_path)



# Algorithms
# ==========

model_database = { os.path.basename(path[:-5]): yaml.safe_load(open(path)) for path in glob('model/*.yaml') }

def get_val_fold(model_root):
    if model_root not in model_database:
        return 'w'
    return model_database[model_root]['training_set']

def remove_small_components(img, stride, largest_only, min_length, padded_boundary=False):
    if padded_boundary:
        img = np.pad(img, 1, 'constant', constant_values=1)
    lab = ndimage.label(img, np.ones((3,3)))[0]
    num = pd.Series()
    for i,s in enumerate(ndimage.find_objects(lab)):
        num.loc[i+1] = np.sum(lab[s]==i+1)
    if len(num):
        max_lab = num.index.max()
        num = num[num * stride >= min_length]
        if largest_only:
            num = num.sort_values().iloc[-1:]
        lab_mask = pd.Series(np.arange(max_lab+1)).isin(num.index).values
        img = lab_mask[lab]
    if padded_boundary:
        img = img[1:-1, 1:-1]
    return img

def get_raster_probability(model_root, iid, green_weight=1):
    if ' ' in model_root:
        components = model_root.split()
        P = 0
        stride = None
        weights = [1] * len(components)
        weights = np.array(weights) / np.sum(weights)
        for w, submodel in zip(weights, components):
            sub_P, sub_stride = get_raster_probability(submodel, iid, green_weight=green_weight)
            P = P + w * sub_P
            assert stride is None or stride == sub_stride
            stride = sub_stride
        return (P, stride)

    # Read model prediction
    model_product = 'rgb_pred_%s_png' % model_root
    P_rgb = np.array(Image.open(iid_path(iid, model_product))) / np.float32(255)
    stride = 4
    need_clipping = model_database[model_root].get('automatic_clipping', 0) >= 2

    # Upscale by 2x
    upscale = 2
    assert stride%upscale == 0
    post_upscale_size = len(P_rgb) * upscale
    stride //= upscale
    P_rgb = np.array(Image.open(iid_path(iid, model_product)).resize((post_upscale_size,)*2, Image.BILINEAR)) / np.float32(255)

    # Clipping
    #if need_clipping:
    #    mask = np.asarray(Image.open(iid_path(iid, 'mask_aniso_png')))[stride//2::stride, stride//2::stride]
    #    P_rgb *= (mask[:,:,None] > 0)

    # Class weighting
    P = P_rgb[:,:,0] + green_weight * P_rgb[:,:,1]

    return (P, stride)

def ensure_no_duplicates(shape):
    """
    This guarantees that the submission is valid even in the hypothetical
    scenario where line simplification might create quasi-duplicate edges.
    Otherwise, this function is a no-op.
    """
    strings = list(as_MultiLineString(shape))
    edges = []
    for strn in strings:
        for u,v in zip(strn.coords, strn.coords[1:]):
            edges.append((u,v))
    edges = np.array(edges)
    edges = edges.round(1)
    num_edges = len(edges)
    edges = { tuple(sorted(map(tuple, edge))) for edge in edges }
    if len(edges) < num_edges:
        print("INFORMATION: a duplicate edge had to be removed")
        edges = np.array(list(edges))
        return linemerge(edges)
    else:
        return shape

def vectorize_skeleton(img, stride, tolerance, preserve_topology=True, remove_hair=0):
    # Extract the image's graph.
    i,j = np.nonzero(img)
    unscaled_xy = np.c_[j,i]
    xy = unscaled_xy * stride * (len(img) / (len(img) - 1)) # minor expansion to ensure exact fit to borders
    xy = xy.round(2)
    try:
        u,v = np.array(list(cKDTree(xy).query_pairs(1.5*stride))).T
    except ValueError:
        return linemerge([])

    # Make sure that no triangles will form at T junctions.
    unscaled_xy_set = set(map(tuple, unscaled_xy))
    unscaled_xy_u = unscaled_xy[u]
    unscaled_xy_v = unscaled_xy[v]
    is_diagonal = np.sum((unscaled_xy_v - unscaled_xy_u)**2, axis=-1) == 2
    keep_mask = ~is_diagonal.copy()
    for k in np.flatnonzero(is_diagonal):
        a = unscaled_xy_u[k]
        b = unscaled_xy_v[k]
        c = (a[0], b[1])
        d = (b[0], a[1])
        if c in unscaled_xy_set or d in unscaled_xy_set:
            keep_mask[k] = False
        else:
            keep_mask[k] = True
    u = u[keep_mask]
    v = v[keep_mask]

    # Convert to Shapely shape.
    lines = np.array([xy[u], xy[v]]).swapaxes(0,1)
    shape = linemerge(lines).simplify(tolerance, preserve_topology=preserve_topology)

    # Remove any short deadends created by skeletonization.
    if remove_hair:
        strings = list(as_MultiLineString(shape))
        arity = {}
        for strn in strings:
            for point in strn.coords:
                arity.setdefault(point, 0)
                arity[point] += 1

        good_strings = []
        for strn in strings:
            if (arity[strn.coords[0]] != 1 and arity[strn.coords[-1]] != 1) \
               or strn.length >= remove_hair:
                good_strings.append(strn)
        shape = MultiLineString(good_strings)

    # Make sure the submission is valid.
    shape = ensure_no_duplicates(shape)

    return shape

ball_5 = np.ones((5,5), dtype=int)
ball_5[0,[0,-1]] = 0
ball_5[-1,[0,-1]] = 0

def binary_denoise(img):
    return ndimage.binary_closing(np.pad(img, 9, mode='reflect'), ball_5)[9:-9,9:-9]

def postprocess(model_root, iid, *, threshold=.288, largest_only=False, min_length=21, margin=4, vectorize_opt=dict(remove_hair=14), green_weight=0.15, padded_boundary=False):
    P_wide, stride = get_raster_probability(model_root, iid, green_weight=green_weight)
    P_wide = np.pad(P_wide, margin, 'edge')
    img = skeletonize(binary_denoise(P_wide >= threshold))
    if margin:
        img = img[margin:-margin, margin:-margin]
    img = remove_small_components(img, stride, largest_only, min_length, padded_boundary=padded_boundary)
    return vectorize_skeleton(img, stride, 1.*stride, **vectorize_opt)

def make_submission(model_root, fold, csv_path):
    rows = []
    #iids = dict_metadata_by_fold(get_val_fold(model_root))[fold].index
    iids = os.listdir('spacenet/Test')
    for iid in tqdm(iids, desc="make %r" % csv_path, unit="images"):
        #ImageId = iid_ImageId(iid)
        iid = os.path.splitext(iid)[0]
        ImageId = iid
        shape = postprocess(model_root, iid)
        linestrings = list(as_MultiLineString(shape))
        if not linestrings:
            rows.append('{ImageId},"{wkt}"'.format(ImageId=ImageId, wkt='LINESTRING EMPTY'))
        else:
            for linestring in linestrings:
                rows.append('{ImageId},"{wkt}"'.format(ImageId=ImageId, wkt=linestring.wkt))

    with open(csv_path, 'w') as f:
        print("ImageId,WKT_Pix", file=f)
        for row in rows:
            print(row, file=f)

def create_images(model_root, fold, csv_path):
    from PIL import Image, ImageDraw
    file = open(csv_path)
    Lines = file.readlines()
    iids = os.listdir('spacenet/Test')
    for iid in tqdm(iids, desc="create_images"):
        out_path = iid_path(iid, "final_jpg")
        with Image.open('spacenet/Test/'+iid) as im:
            draw = ImageDraw.Draw(im)
            iid = os.path.splitext(iid)[0]
            ImageId = iid
            for line in Lines:
                ls = line.strip()
                if ImageId+"," in ls:
                    if re.search(r'LINESTRING \(([0-9. ,]+)\)', ls):
                        strn, = re.search(r'LINESTRING \(([0-9. ,]+)\)', ls).groups()
                        strn = np.array(strn.replace(',', ' ').split(), dtype=float).reshape(-1, 2)
                        assert len(strn) >= 2
                        for a,b in zip(strn[:-1], strn[1:]):
                            draw.line((*a, *b), fill=(255, 0, 0),width=3)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        im.save(out_path)


if __name__=='__main__':
    import sys

    if sys.platform != 'win32':
        from PIL import __version__; assert __version__>"4.3.0.post0"

    args = sys.argv[1:]
    force_rebuild = '--force-rebuild' in args
    if '--provision' in args:
        build_dist_aniso(force_rebuild=force_rebuild)
        #build_rgb_aniso(force_rebuild=force_rebuild)
        #build_mask_aniso(force_rebuild=force_rebuild)
    else:
        raise NotImplementedError
