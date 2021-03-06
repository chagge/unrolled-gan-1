import numpy as np
import os, sys, time, math
from chainer import cuda
from chainer import functions as F
import pandas as pd
sys.path.append(os.path.split(os.getcwd())[0])
import dataset
from progress import Progress
from mnist_tools import load_train_images, load_test_images
from model import discriminator_params, generator_params, gan
from args import args
from plot import plot

def get_learning_rate_for_epoch(epoch):
	if epoch < 10:
		return 0.001
	if epoch < 50:
		return 0.0003
	return 0.0001

def main():
	# load MNIST images
	images, labels = dataset.load_train_images()

	# config
	discriminator_config = gan.config_discriminator
	generator_config = gan.config_generator

	# settings
	# _l -> labeled
	# _u -> unlabeled
	# _g -> generated
	max_epoch = 1000
	num_updates_per_epoch = 500
	plot_interval = 5
	batchsize_l = 100
	batchsize_u = 100
	batchsize_g = batchsize_u

	# seed
	np.random.seed(args.seed)
	if args.gpu_device != -1:
		cuda.cupy.random.seed(args.seed)

	# save validation accuracy per epoch
	csv_results = []

	# create semi-supervised split
	num_validation_data = 10000
	num_labeled_data = args.num_labeled
	if batchsize_l > num_labeled_data:	# correct labeled batchsize
		batchsize_l = num_labeled_data

	training_images_l, training_labels_l, training_images_u, validation_images, validation_labels = dataset.create_semisupervised(images, labels, num_validation_data, num_labeled_data, discriminator_config.ndim_output, seed=args.seed)
	print training_labels_l

	# training
	progress = Progress()
	for epoch in xrange(1, max_epoch + 1):
		progress.start_epoch(epoch, max_epoch)
		sum_loss_supervised = 0
		sum_loss_unsupervised = 0
		sum_loss_generator = 0
		sum_dx_labeled = 0
		sum_dx_unlabeled = 0
		sum_dx_generated = 0

		gan.update_learning_rate(get_learning_rate_for_epoch(epoch))

		for t in xrange(num_updates_per_epoch):
			# unrolling
			for k in xrange(discriminator_config.unrolling_steps):
				# sample true data from data distribution
				images_l, label_onehot_l, label_ids_l = dataset.sample_labeled_data(training_images_l, training_labels_l, batchsize_l, discriminator_config.ndim_input, discriminator_config.ndim_output, binarize=False)
				images_u = dataset.sample_unlabeled_data(training_images_u, batchsize_u, discriminator_config.ndim_input, binarize=False)
				# sample fake data from generator
				images_g = gan.generate_x(batchsize_g)
				images_g.unchain_backward()

				# supervised loss
				py_x_l, activations_l = gan.discriminate(images_l, apply_softmax=False)
				loss_supervised = F.softmax_cross_entropy(py_x_l, gan.to_variable(label_ids_l))

				log_zx_l = F.logsumexp(py_x_l, axis=1)
				log_dx_l = log_zx_l - F.softplus(log_zx_l)		# logD(x)
				dx_l = F.sum(F.exp(log_dx_l)) / batchsize_l		# D(x)

				# unsupervised loss
				# D(x) = Z(x) / {Z(x) + 1}, where Z(x) = \sum_{k=1}^K exp(l_k(x))
				# softplus(x) := log(1 + exp(x))
				# logD(x) = logZ(x) - log(Z(x) + 1)
				# 		  = logZ(x) - log(exp(log(Z(x))) + 1)
				# 		  = logZ(x) - softplus(logZ(x))
				# 1 - D(x) = 1 / {Z(x) + 1}
				# log{1 - D(x)} = log1 - log(Z(x) + 1)
				# 				= -log(exp(log(Z(x))) + 1)
				# 				= -softplus(logZ(x))
				py_x_u, _ = gan.discriminate(images_u, apply_softmax=False)
				log_zx_u = F.logsumexp(py_x_u, axis=1)
				log_dx_u = log_zx_u - F.softplus(log_zx_u)
				dx_u = F.sum(F.exp(log_dx_u)) / batchsize_u
				loss_unsupervised = -F.sum(log_dx_u) / batchsize_u	# minimize negative logD(x)
				py_x_g, _ = gan.discriminate(images_g, apply_softmax=False)
				log_zx_g = F.logsumexp(py_x_g, axis=1)
				loss_unsupervised += F.sum(F.softplus(log_zx_g)) / batchsize_u	# minimize negative log{1 - D(x)}

				# update discriminator k times
				gan.backprop_discriminator(loss_supervised + loss_unsupervised)

				if k == 0:
					gan.cache_discriminator_weights()
					sum_loss_supervised += float(loss_supervised.data)
					sum_loss_unsupervised += float(loss_unsupervised.data)
					sum_dx_labeled += float(dx_l.data)
					sum_dx_unlabeled += float(dx_u.data)

			# generator loss
			images_g = gan.generate_x(batchsize_g)
			py_x_g, activations_g = gan.discriminate(images_g, apply_softmax=False)
			log_zx_g = F.logsumexp(py_x_g, axis=1)
			log_dx_g = log_zx_g - F.softplus(log_zx_g)
			dx_g = F.sum(F.exp(log_dx_g)) / batchsize_g
			loss_generator = -F.sum(log_dx_g) / batchsize_u	# minimize negative logD(x)

			# feature matching
			if discriminator_config.use_feature_matching:
				features_true = activations_l[-1]
				features_true.unchain_backward()
				if batchsize_l != batchsize_g:
					images_g = gan.generate_x(batchsize_l)
					_, activations_g = gan.discriminate(images_g, apply_softmax=False)
				features_fake = activations_g[-1]
				loss_generator += F.mean_squared_error(features_true, features_fake)

			# update generator
			gan.backprop_generator(loss_generator)

			# update discriminator with updates at k = 0
			gan.restore_discriminator_weights()

			sum_loss_generator += float(loss_generator.data)
			sum_dx_generated += float(dx_g.data)
			if t % 10 == 0:
				progress.show(t, num_updates_per_epoch, {})

		gan.save(args.model_dir)

		# validation
		images_l, _, label_ids_l = dataset.sample_labeled_data(validation_images, validation_labels, num_validation_data, discriminator_config.ndim_input, discriminator_config.ndim_output, binarize=False)
		images_l_segments = np.split(images_l, num_validation_data // 500)
		label_ids_l_segments = np.split(label_ids_l, num_validation_data // 500)
		sum_accuracy = 0
		for images_l, label_ids_l in zip(images_l_segments, label_ids_l_segments):
			y_distribution, _ = gan.discriminate(images_l, apply_softmax=True, test=True)
			accuracy = F.accuracy(y_distribution, gan.to_variable(label_ids_l))
			sum_accuracy += float(accuracy.data)
		validation_accuracy = sum_accuracy / len(images_l_segments)
		
		progress.show(num_updates_per_epoch, num_updates_per_epoch, {
			"loss_l": sum_loss_supervised / num_updates_per_epoch,
			"loss_u": sum_loss_unsupervised / num_updates_per_epoch,
			"loss_g": sum_loss_generator / num_updates_per_epoch,
			"dx_l": sum_dx_labeled / num_updates_per_epoch,
			"dx_u": sum_dx_unlabeled / num_updates_per_epoch,
			"dx_g": sum_dx_generated / num_updates_per_epoch,
			"accuracy": validation_accuracy,
		})

		# write accuracy to csv
		csv_results.append([epoch, validation_accuracy, progress.get_total_time()])
		data = pd.DataFrame(csv_results)
		data.columns = ["epoch", "accuracy", "min"]
		data.to_csv("{}/result.csv".format(args.model_dir))

		if epoch % plot_interval == 0 or epoch == 1:
			plot(filename="epoch_{}_time_{}min".format(epoch, progress.get_total_time()))

if __name__ == "__main__":
	main()
