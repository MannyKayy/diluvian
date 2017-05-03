# -*- coding: utf-8 -*-


import itertools
import Queue

import matplotlib.animation as animation
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

from .config import CONFIG
from .octrees import OctreeVolume
from .postprocessing import Body
from .util import (
        get_color_shader,
        pad_dims,
        WrappedViewer,
        )


class DenseRegion(object):
    @staticmethod
    def from_subvolume(subvolume):
        if subvolume.label_mask is not None and np.issubdtype(subvolume.label_mask.dtype, np.bool):
            target = mask_to_output_target(subvolume.label_mask)
        else:
            target = subvolume.label_mask
        return DenseRegion(subvolume.image,
                           target=target,
                           seed_vox=subvolume.seed)

    @staticmethod
    def from_subvolume_generator(subvolumes):
        subvolumes = itertools.ifilter(lambda s: s.has_uniform_seed_margin, subvolumes)
        return itertools.imap(DenseRegion.from_subvolume, subvolumes)

    def __init__(self, image, target=None, seed_vox=None, mask=None):
        self.MOVE_DELTA = (CONFIG.model.output_fov_shape - 1) / CONFIG.model.output_fov_move_fraction
        self.MOVE_GRID_OFFSET = np.mod(seed_vox, self.MOVE_DELTA).astype('int64')
        self.queue = Queue.PriorityQueue()
        self.visited = set()
        self.image = image
        self.bounds = image.shape
        self.move_bounds = self.vox_to_pos(self.bounds) - 1
        self.move_check_thickness = CONFIG.model.move_check_thickness
        if mask is None:
            if isinstance(self.image, OctreeVolume):
                self.mask = OctreeVolume(self.image.leaf_shape, (np.zeros(3), self.bounds), 'float32')
            else:
                self.mask = np.empty(self.bounds, dtype='float32')
            self.mask[:] = np.NAN
        else:
            self.mask = mask
        self.target = target
        self.bias_against_merge = False
        self.move_based_on_new_mask = False
        if seed_vox is None:
            seed_pos = np.floor_divide(self.move_bounds, 2) + 1
        else:
            seed_pos = self.vox_to_pos(seed_vox)
            assert self.pos_in_bounds(seed_pos), \
                'Seed position (%s) must be in region bounds (%s).' % (seed_vox, self.bounds)
        self.seed_pos = seed_pos
        self.queue.put((None, seed_pos))
        seed_vox = self.pos_to_vox(seed_pos)
        if self.target is not None:
            np.testing.assert_almost_equal(self.target[tuple(seed_vox)], CONFIG.model.v_true,
                                           err_msg='Seed position should be in target body.')
        self.mask[tuple(seed_vox)] = CONFIG.model.v_true

    def unfilled_copy(self):
        copy = DenseRegion(self.image, self.target, self.seed_pos)
        copy.bias_against_merge = self.bias_against_merge
        copy.move_based_on_new_mask = self.move_based_on_new_mask

    def to_body(self):
        def threshold(a):
            return a >= CONFIG.model.t_final

        if isinstance(self.mask, OctreeVolume):
            hard_mask = self.mask.map_copy(np.bool, threshold, threshold)
        else:
            hard_mask = threshold(self.mask)

        return Body(hard_mask, self.pos_to_vox(self.seed_pos))

    def vox_to_pos(self, vox):
        return np.floor_divide(vox - self.MOVE_GRID_OFFSET, self.MOVE_DELTA).astype('int64')

    def pos_to_vox(self, pos):
        return (pos * self.MOVE_DELTA).astype('int64') + self.MOVE_GRID_OFFSET

    def pos_in_bounds(self, pos):
        return np.all(np.less(pos, self.move_bounds)) and np.all(pos > 1)

    def get_moves(self, mask):
        moves = []
        ctr = (np.asarray(mask.shape) - 1) / 2 + 1
        for move in map(np.array, [(1, 0, 0), (-1, 0, 0),
                                   (0, 1, 0), (0, -1, 0),
                                   (0, 0, 1), (0, 0, -1)]):
            plane_min = ctr - (-2 * np.maximum(move, 0) + 1) * self.MOVE_DELTA \
                            - np.abs(move) * (self.move_check_thickness - 1)
            plane_max = ctr + (+2 * np.minimum(move, 0) + 1) * self.MOVE_DELTA \
                            + np.abs(move) * (self.move_check_thickness - 1) + 1
            moves.append({'move': move,
                          'v': mask[plane_min[0]:plane_max[0],
                                    plane_min[1]:plane_max[1],
                                    plane_min[2]:plane_max[2]].max()})
        return moves

    def add_mask(self, mask_block, mask_pos):
        mask_origin = self.pos_to_vox(mask_pos) - (np.asarray(mask_block.shape) - 1) / 2
        current_mask = self.mask[mask_origin[0]:mask_origin[0] + mask_block.shape[0],
                                 mask_origin[1]:mask_origin[1] + mask_block.shape[1],
                                 mask_origin[2]:mask_origin[2] + mask_block.shape[2]]

        if self.bias_against_merge:
            update_mask = np.isnan(current_mask) | (current_mask > 0.5) | np.less(mask_block, current_mask)
            current_mask[update_mask] = mask_block[update_mask]
        else:
            current_mask[:] = mask_block

        self.mask[mask_origin[0]:mask_origin[0] + mask_block.shape[0],
                  mask_origin[1]:mask_origin[1] + mask_block.shape[1],
                  mask_origin[2]:mask_origin[2] + mask_block.shape[2]] = current_mask

        if self.move_based_on_new_mask:
            new_moves = self.get_moves(mask_block)
        else:
            new_moves = self.get_moves(current_mask)
        for move in new_moves:
            new_pos = mask_pos + move['move']
            if not self.pos_in_bounds(new_pos):
                continue
            if tuple(new_pos) not in self.visited and move['v'] >= CONFIG.model.t_move:
                self.visited.add(tuple(new_pos))
                self.queue.put((-move['v'], tuple(new_pos)))

    def get_next_block(self):
        next_pos = np.asarray(self.queue.get()[1])
        next_vox = self.pos_to_vox(next_pos)
        margin = (CONFIG.model.input_fov_shape - 1) / 2
        block_min = next_vox - margin
        block_max = next_vox + margin + 1
        image_block = self.image[block_min[0]:block_max[0],
                                 block_min[1]:block_max[1],
                                 block_min[2]:block_max[2]]
        mask_block = self.mask[block_min[0]:block_max[0],
                               block_min[1]:block_max[1],
                               block_min[2]:block_max[2]].copy()

        mask_block[np.isnan(mask_block)] = CONFIG.model.v_false

        if self.target is not None:
            margin = (CONFIG.model.output_fov_shape - 1) / 2
            oblock_min = next_vox - margin
            oblock_max = next_vox + margin + 1
            target_block = self.target[oblock_min[0]:oblock_max[0],
                                       oblock_min[1]:oblock_max[1],
                                       oblock_min[2]:oblock_max[2]]
        else:
            target_block = None

        assert image_block.shape == tuple(CONFIG.model.input_fov_shape), \
            'Image wrong shape: {}'.format(image_block.shape)
        assert mask_block.shape == tuple(CONFIG.model.input_fov_shape), \
            'Mask wrong shape: {}'.format(mask_block.shape)
        return {'image': image_block,
                'mask': mask_block,
                'target': target_block,
                'position': next_pos}

    def fill(self, model, verbose=False, move_batch_size=1, max_moves=None, multi_gpu_pad_kludge=None):
        moves = 0
        if verbose:
            pbar = tqdm(desc='Move queue')
        while not self.queue.empty():
            batch_block_data = [self.get_next_block() for _ in
                                itertools.takewhile(lambda _: not self.queue.empty(), range(move_batch_size))]
            batch_moves = len(batch_block_data)
            if verbose:
                moves += batch_moves
                pbar.total = moves + self.queue.qsize()
                pbar.set_description('Move ' + str(batch_block_data[-1]['position']))
                pbar.update(batch_moves)

            image_input = np.concatenate([pad_dims(b['image']) for b in batch_block_data])
            mask_input = np.concatenate([pad_dims(b['mask']) for b in batch_block_data])

            # For models generated with make_parallel that saved the parallel
            # model, not the original model, some kludge is necessary so that
            # the batch size is large enough to give each GPU in the parallel
            # model an equal number of samples.
            if multi_gpu_pad_kludge is not None and image_input.shape[0] % multi_gpu_pad_kludge != 0:
                missing_samples = multi_gpu_pad_kludge - (image_input.shape[0] % multi_gpu_pad_kludge)
                fill_dim = list(image_input.shape)
                fill_dim[0] = missing_samples
                image_input = np.concatenate((image_input, np.zeros(fill_dim, dtype=image_input.dtype)))
                mask_input = np.concatenate((mask_input, np.zeros(fill_dim, dtype=mask_input.dtype)))

            output = model.predict({'image_input': image_input,
                                    'mask_input': mask_input})

            for ind, block_data in enumerate(batch_block_data):
                self.add_mask(output[ind, :, :, :, 0], block_data['position'])

            if max_moves is not None and moves > max_moves:
                break

        if verbose:
            pbar.close()

    def fill_animation(self, model, movie_filename, verbose=False):
        dpi = 100
        fig = plt.figure(figsize=(1920/dpi, 1080/dpi), dpi=dpi)
        fig.patch.set_facecolor('black')
        axes = {
            'xy': fig.add_subplot(1, 3, 1),
            'xz': fig.add_subplot(1, 3, 2),
            'zy': fig.add_subplot(1, 3, 3),
        }

        def get_plane(arr, vox, plane):
            return {
                'xy': lambda a, v: np.transpose(a[:, :, v[2]]),
                'xz': lambda a, v: np.transpose(a[:, v[1], :]),
                'zy': lambda a, v: a[v[0], :, :],
            }[plane](arr, np.round(vox).astype('int64'))

        def get_hv(vox, plane):
            # rel = np.divide(vox, self.bounds)
            rel = vox
            # rel = self.bounds - vox
            return {
                'xy': {'h': rel[1], 'v': rel[0]},
                'xz': {'h': rel[2], 'v': rel[0]},
                'zy': {'h': rel[1], 'v': rel[2]},
            }[plane]

        def get_aspect(plane):
            return {
                'xy': float(CONFIG.volume.resolution[1]) / float(CONFIG.volume.resolution[0]),
                'xz': float(CONFIG.volume.resolution[2]) / float(CONFIG.volume.resolution[0]),
                'zy': float(CONFIG.volume.resolution[1]) / float(CONFIG.volume.resolution[2]),
            }[plane]

        images = {
            'image': {},
            'mask': {},
        }
        lines = {
            'v': {},
            'h': {},
            'bl': {},
            'bt': {},
        }
        current_vox = self.pos_to_vox(self.seed_pos)
        margin = (CONFIG.model.input_fov_shape) / 2
        for plane, ax in axes.iteritems():
            ax.get_xaxis().set_visible(False)
            ax.get_yaxis().set_visible(False)

            image_data = get_plane(self.image, current_vox, plane)
            im = ax.imshow(image_data, cmap='gray')
            im.set_clim([0, 1])
            images['image'][plane] = im

            mask_data = get_plane(self.mask, current_vox, plane)
            im = ax.imshow(mask_data, cmap='jet', alpha=0.8)
            im.set_clim([0, 1])
            images['mask'][plane] = im

            aspect = get_aspect(plane)
            lines['h'][plane] = ax.axhline(y=get_hv(current_vox - margin, plane)['h'], color='w')
            lines['v'][plane] = ax.axvline(x=get_hv(current_vox + margin, plane)['v'], color='w')
            lines['bl'][plane] = ax.axvline(x=get_hv(current_vox - margin, plane)['v'], color='w')
            lines['bt'][plane] = ax.axhline(y=get_hv(current_vox + margin, plane)['h'], color='w')

            ax.set_aspect(aspect)

        plt.tight_layout()

        def update_fn(vox):
            if np.array_equal(np.round(vox).astype('int64'), update_fn.next_pos_vox):
                if update_fn.block_data is not None:
                    image_input = pad_dims(update_fn.block_data['image'])
                    mask_input = pad_dims(update_fn.block_data['mask'])

                    output = model.predict({'image_input': image_input,
                                            'mask_input': mask_input})
                    self.add_mask(output[0, :, :, :, 0], update_fn.block_data['position'])

                if not self.queue.empty():
                    update_fn.moves += 1
                    if verbose:
                        update_fn.pbar.total = update_fn.moves + self.queue.qsize()
                        update_fn.pbar.update()
                    update_fn.block_data = self.get_next_block()

                    update_fn.next_pos_vox = self.pos_to_vox(update_fn.block_data['position'])
                    if not np.array_equal(np.round(vox).astype('int64'), update_fn.next_pos_vox):
                        p = update_fn.next_pos_vox - vox
                        steps = np.linspace(0, 1, 16)
                        interp_vox = vox + np.outer(steps, p)
                        for row in interp_vox:
                            update_fn.vox_queue.put(row)
                    else:
                        update_fn.vox_queue.put(vox)

            for plane, im in images['image'].iteritems():
                image_data = get_plane(self.image, vox, plane)
                im.set_data(image_data)

            for plane, im in images['mask'].iteritems():
                image_data = get_plane(self.mask, vox, plane)
                masked_data = np.ma.masked_where(image_data < 0.5, image_data)
                im.set_data(masked_data)

            for plane in axes.iterkeys():
                lines['h'][plane].set_ydata(get_hv(vox - margin, plane)['h'])
                lines['v'][plane].set_xdata(get_hv(vox + margin, plane)['v'])
                lines['bl'][plane].set_xdata(get_hv(vox - margin, plane)['v'])
                lines['bt'][plane].set_ydata(get_hv(vox + margin, plane)['h'])

            return images['image'].values() + images['mask'].values() + \
                lines['h'].values() + lines['v'].values() + \
                lines['bl'].values() + lines['bt'].values()

        update_fn.moves = 0
        update_fn.block_data = None
        if verbose:
            update_fn.pbar = tqdm(desc='Move queue')

        update_fn.next_pos_vox = current_vox
        update_fn.vox_queue = Queue.Queue()
        update_fn.vox_queue.put(current_vox)

        def vox_gen():
            last_vox = None
            while 1:
                if update_fn.vox_queue.empty():
                    return
                else:
                    last_vox = update_fn.vox_queue.get()
                    yield last_vox

        ani = animation.FuncAnimation(fig, update_fn, frames=vox_gen(), interval=16, repeat=False, save_count=60*60)
        writer = animation.writers['ffmpeg'](fps=60)

        ani.save(movie_filename, writer=writer, dpi=dpi, savefig_kwargs={'facecolor': 'black'})

        if verbose:
            update_fn.pbar.close()

        return ani

    def get_viewer(self, transpose=False):
        if transpose:
            viewer = WrappedViewer(voxel_size=list(CONFIG.volume.resolution),
                                   voxel_coordinates=self.pos_to_vox(self.seed_pos))
            viewer.add(np.transpose(self.image),
                       name='Image')
            if self.target is not None:
                viewer.add(np.transpose(self.target),
                           name='Mask Target',
                           shader=get_color_shader(0))
            viewer.add(np.transpose(self.mask),
                       name='Mask Output',
                       shader=get_color_shader(1))
        else:
            viewer = WrappedViewer(voxel_size=list(np.flipud(CONFIG.volume.resolution)),
                                   voxel_coordinates=np.flipud(self.pos_to_vox(self.seed_pos)))
            viewer.add(self.image,
                       name='Image')
            if self.target is not None:
                viewer.add(self.target,
                           name='Mask Target',
                           shader=get_color_shader(0))
            viewer.add(self.mask,
                       name='Mask Output',
                       shader=get_color_shader(1))
        return viewer


def mask_to_output_target(mask):
    target = np.full_like(mask, CONFIG.model.v_false, dtype='float32')
    target[mask] = CONFIG.model.v_true
    return target
