import os
import argparse
import pickle
import glob
import numpy as np
from PIL import Image

import torch
from torchvision.transforms import functional as F
from torch.nn import functional as FF
from torch.utils.data import Dataset, DataLoader
from facenet_pytorch import MTCNN, extract_face,\
                            InceptionResnetV1, fixed_image_standardization

from utils import crop_resize_back, compute_dist,\
                  tensor_to_image, array_to_image

import logging

logger = logging.getLogger(__name__)


def preprocess_image(image_path):
    "Load Image, normalize and convert to tensor."
    img = Image.open(image_path)
    img_tensor = F.to_tensor(np.float32(img))
    return fixed_image_standardization(image_tensor=img_tensor)  # in [-1, 1]


class PairDataset(Dataset):
    def __init__(self, source_image_paths, target_image_paths):
        super().__init__()
        assert len(source_image_paths) == len(target_image_paths)
        self.source_image_paths = source_image_paths
        self.target_image_paths = target_image_paths

    def __len__(self):
        return len(self.source_image_paths)

    def __getitem__(self, idx):
        source = self.source_image_paths[idx]
        target = self.target_image_paths[idx]
        source_tensor = preprocess_image(source)
        target_tensor = preprocess_image(target)
        return source_tensor, target_tensor, source, target


def customized_collate_fn(batch_list):
    "A customized collate_fn function for Dataloader."
    source = torch.cat([item[0].unsqueeze(0) for item in batch_list], dim=0)
    target = torch.cat([item[1].unsqueeze(0) for item in batch_list], dim=0)
    source_paths = [item[2].unsqueeze(0) for item in batch_list]
    target_paths = [item[3].unsqueeze(0) for item in batch_list]
    return source, target, source_paths, target_paths


def fixed_image_standardization_inverse(image_tensor):
    return image_tensor * 128.0 + 127.5


def extraction(image_dir, mtcnn):
    files = os.listdir(image_dir)
    for file in files:
        if not file.endswith('png'):
            continue
        file_path = os.path.join(image_dir, file)
        img_origin = Image.open(file_path)
        id = file.split('.')[0]
        _, box_size, box = mtcnn(
            img_origin,
            save_path=os.path.join('{}_cropped'.format(image_dir), '{}_cropped.png'.format(id)))

        # save box_size and box for future recovery.
        img_meta_dict = {'id': id, 'box_size': box_size, 'box': box}
        pickle.dump(img_meta_dict,
                    open(os.path.join('{}_cropped'.format(image_dir), '{}_info.pkl'.format(id)), 'wb'))


def face_extraction(args):
    mtcnn = MTCNN(image_size=args.image_size)
    extraction('val', mtcnn)
    extraction('test', mtcnn)


def get_pretrained_inception_model(dataset='vggface2'):
    if dataset == 'vggface2':
        # For a model pretrained on VGGFace2
        model = InceptionResnetV1(pretrained='vggface2')
    elif dataset == 'casia-webface':
        # For a model pretrained on CASIA-Webface
        model = InceptionResnetV1(pretrained='casia-webface')
    return model


def cal_source_grad(inception_model, source_img, target_rep):
    source_img.requires_grad = True
    source_rep = inception_model(source_img)

    similarity = (target_rep * source_rep).sum(dim=1).mean()

    inception_model.zero_grad()
    # cal gradient
    similarity.backward()

    return source_img.grad.data, similarity.cpu().item()


def iterative_grad_attack(inception_model, source_img, target_img,
                          n_steps=50, lr=0.01):
    with torch.no_grad():
        target_rep = inception_model(target_img).detach()

    perturbed_img = source_img.clone()
    for step in range(1, n_steps+1):
        grad, similarity = cal_source_grad(inception_model, perturbed_img, target_rep)
        # print('grad range ', grad.max(), grad.min())
        perturbed_img = perturbed_img + lr * grad
        perturbed_img = torch.clamp(perturbed_img, -1.0, 1.0).detach_()
        if similarity > 0.99:
            break
        # if step % 50 == 1:
        #     print('step {}, similarity: {:.4f}'.format(step, similarity.item()))
    adv_rep = inception_model(perturbed_img)
    rep_dist = (target_rep * adv_rep).sum(dim=1).mean().cpu().item()

    adv_imgs = [tensor_to_image(img.squeeze(0)) for img in fixed_image_standardization_inverse(perturbed_img.cpu()).split(split_size=1, dim=0)]
    target_imgs = [tensor_to_image(img.squeeze(0)) for img in fixed_image_standardization_inverse(target_img.cpu()).split(split_size=1, dim=0)]

    dist_list = [compute_dist(np.asarray(adv_img), np.asarray(target_img)) for (adv_img, target_img) in zip(adv_imgs, target_imgs)]
    dist = np.mean(dist_list)
    return adv_imgs, dist, rep_dist


def attack(args, mode='val'):
    # Create an inception resnet (in eval mode):
    resnet = InceptionResnetV1(pretrained=args.dataset_pretrained).eval()
    resnet = resnet.cuda()

    np.random.seed(args.seed)
    if mode == 'val':
        logger.info('==> Attach on Val Set')
        print('==> Attach on Val Set')
        image_path_list = glob.glob('val_cropped/*_cropped.png')
        image_path_list_shuffle = np.random.permutation(image_path_list)
        pixel_dist_list = []
        rep_dist_list = []
        original_pixel_dist_list = []
        log_dir = os.path.join(args.log_dir, 'val')
        if not os.path.isdir(log_dir):
            os.mkdir(log_dir)

        # pair_out = open(os.path.join(log_dir, 'pair.txt'), 'w')
        pair_list = []
        dataset = PairDataset(image_path_list_shuffle, image_path_list)
        dataloader = DataLoader(dataset, batch_size=args.attack_batch_size,
                                shuffle=False,
                                drop_last=False, num_workers=8)

        for batch_idx, (source_img, target_img, src_paths, tgt_paths) in enumerate(dataloader):
            # source_img = preprocess_image(source_path)
            # target_img = preprocess_image(target_path)
            # if batch_idx % 20 == 1:
            #     logger.info('batch {} finished '.format(batch_idx))
                # print('batch {} finished '.format(batch_idx))
            source_img = source_img.cuda()
            target_img = target_img.cuda()

            adv_imgs, dist, rep_dist = iterative_grad_attack(
                resnet, source_img, target_img,
                lr=args.attack_lr, n_steps=args.attack_steps)

            pixel_dist_list.append(dist)
            rep_dist_list.append(rep_dist)
            print('sample {}, rep_similarity: {:.3f}'.format(batch_idx+1, rep_dist))
            for idx, (source_path, target_path) in enumerate(zip(src_paths, tgt_paths)):
                source_id = source_path.split('/')[-1][:4]
                target_id = target_path.split('/')[-1][:4]
                source_name = '{}_adv.png'.format(source_id)
                target_name = '{}.png'.format(target_id)

                # crop resize back and save.
                origin_img = Image.open('val/{}.png'.format(source_id))
                info_img = pickle.load(open('val_cropped/{}_info.pkl'.format(source_id), 'rb'))

                adv_img_full = crop_resize_back(origin_img, adv_imgs[idx], info_img['box'], info_img['box_size'])
                adv_img_full.save(os.path.join(log_dir, source_name))

                original_pixel_dist_list.append(compute_dist(np.asarray(adv_img_full), np.asarray(origin_img)))
                pair_list.append((source_name, target_name))
        pickle.dump(pair_list, open(os.path.join(log_dir, 'pair.pickle'), 'wb'))
        #         pair_out.write('{} {}\n'.format(source_name, target_name))
        # pair_out.close()
    else:
        logger.info('==> Attach on Test Set')
        print('==> Attach on Test Set')
        pixel_dist_list = []
        rep_dist_list = []
        original_pixel_dist_list = []
        log_dir = os.path.join(args.log_dir, 'test')
        if not os.path.isdir(log_dir):
            os.mkdir(log_dir)

        pair_in = open('test/pair.txt', 'r')
        lines = pair_in.readlines()
        src_paths = [line.strip().split(' ')[0] for line in lines]
        src_paths = ['test_cropped/{}_cropped.png'.format(path[:4]) for path in src_paths]

        tgt_paths = [line.strip().split(' ')[1] for line in lines]
        tgt_paths = ['test_cropped/{}_cropped.png'.format(path[:4]) for path in tgt_paths]

        dataset = PairDataset(src_paths, tgt_paths)
        dataloader = DataLoader(dataset, batch_size=args.attack_batch_size,
                                shuffle=False,
                                drop_last=False, num_workers=8)

        for batch_idx, (source_img, target_img, src_paths, tgt_paths) in enumerate(dataloader):
            # if batch_idx % 20 == 19:
            #     logger.info('batch {} finished '.format(batch_idx))
            source_img = source_img.cuda()
            target_img = target_img.cuda()

            adv_imgs, dist, rep_dist = iterative_grad_attack(
                resnet, source_img, target_img,
                lr=args.attack_lr, n_steps=args.attack_steps)

            pixel_dist_list.append(dist)
            rep_dist_list.append(rep_dist)
            print('sample {}, rep_similarity: {:.3f}'.format(batch_idx+1, rep_dist))
            for idx, (source_path, target_path) in enumerate(zip(src_paths, tgt_paths)):
                source_id = source_path.split('/')[-1][:4]
                target_id = target_path.split('/')[-1][:4]
                source_name = '{}_adv.png'.format(source_id)
                target_name = '{}.png'.format(target_id)

                # crop resize back and save.
                origin_img = Image.open('test/{}.png'.format(source_id))
                info_img = pickle.load(open('test_cropped/{}_info.pkl'.format(source_id), 'rb'))

                adv_img_full = crop_resize_back(origin_img, adv_imgs[idx], info_img['box'], info_img['box_size'])
                adv_img_full.save(os.path.join(log_dir, source_name))

                original_pixel_dist_list.append(compute_dist(np.asarray(adv_img_full), np.asarray(origin_img)))

    return np.mean(original_pixel_dist_list), np.mean(pixel_dist_list), np.mean(rep_dist_list)


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Webank Attack of Face Recognition")
    parser.add_argument("--face_extraction", action="store_true",
                        help="Extract faces with MTCNN.")
    parser.add_argument("--rejection_inference", action="store_true",
                        help="Used in inference mode with rejection")
    parser.add_argument("--ood_inference", action="store_true",
                        help="Used in ood inference mode")
    parser.add_argument("--log_dir", type=str,
                        default='./logs', help="Location to save logs")

    # Dataset hyperparams:
    parser.add_argument("--problem", type=str, default='cifar10',
                        help="Problem (mnist/fashion/cifar10")
    parser.add_argument("--n_classes", type=int,
                        default=10, help="number of classes of dataset.")
    parser.add_argument("--dataset_pretrained", type=str, default='vggface2',
                        help="Dataset that Inception Model is\
                         pretrained: ['vggface2', 'casia-webface']")

    # Optimization hyperparams:
    parser.add_argument("--attack_batch_size", type=int,
                        default=1, help="attack batch size")
    parser.add_argument("--n_batch_test", type=int,
                        default=200, help="Minibatch size")
    parser.add_argument("--optimizer", type=str,
                        default="adam", help="adam or adamax")
    parser.add_argument("--attack_lr", type=float, default=1.,
                        help="Attack learning rate")
    parser.add_argument("--attack_steps", type=int, default=300,
                        help="Number of iterative attack steps")
    parser.add_argument("--lr", type=float, default=0.001,
                        help="Base learning rate")
    parser.add_argument("--epochs", type=int, default=100,
                        help="Total number of training epochs")

    # Model hyperparams:
    parser.add_argument("--image_size", type=int,
                        default=160, help="Output image size of MTCNN, i.e. the input size of InceptionResnet.")
    parser.add_argument("--margin", type=float, default=5,
                        help="margin")
    parser.add_argument("--encoder_name", type=str, default='resnet26',
                        help="encoder name: resnet#")
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')

    # Ablation
    parser.add_argument("--seed", type=int, default=1234, help="Random seed")

    args = parser.parse_args()

    if os.path.exists(args.log_dir) is False:
        os.mkdir(args.log_dir)

    if os.path.exists('val_cropped') is False:
        os.mkdir('val_cropped')

    if os.path.exists('test_cropped') is False:
        os.mkdir('test_cropped')

    if os.path.exists(os.path.join(args.log_dir, 'val')) is False:
        os.mkdir(os.path.join(args.log_dir, 'val'))

    if os.path.exists(os.path.join(args.log_dir, 'test')) is False:
        os.mkdir(os.path.join(args.log_dir, 'test'))

    if args.face_extraction:
        face_extraction(args)
    else:
        pixel_dist, pixel_crop_dist, rep_dist = attack(args, 'val')
        print('=>[Val] pixel dist: {:.3f}, pixel_crop_dist: {:.3f}, rep_dist: {:.3f}'.format(pixel_dist, pixel_crop_dist, rep_dist))
        pixel_dist, pixel_crop_dist, rep_dist = attack(args, 'test')
        print('=>[Test] pixel dist: {:.3f}, pixel_crop_dist: {:.3f}, rep_dist: {:.3f}'.format(pixel_dist, pixel_crop_dist, rep_dist))
