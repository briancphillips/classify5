"""Example script for running traditional classifier experiments."""

import logging
from pathlib import Path
import sys
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from tqdm import tqdm

from config.experiment_config import create_experiment_config
from experiments.traditional import TraditionalExperiment
from models.data import get_dataset
from models.factory import get_model
from utils.logging import setup_logging, get_logger

logger = get_logger(__name__)

CHECKPOINT_PATH = "checkpoints/wideresnet/wideresnet_best.pt"


def train_model(model, train_loader, val_loader, device, epochs=200):
    """Train model from scratch using WideResNet paper settings.

    Settings from paper:
    - SGD with momentum 0.9
    - Weight decay 5e-4
    - Initial learning rate 0.1, divided by 5 at 60, 120, and 160 epochs
    - Batch size 128
    - 200 epochs total
    - Standard data augmentation (horizontal flip, random crop)
    """
    logger.info("Training model from scratch using WideResNet paper settings...")

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(
        model.parameters(),
        lr=0.1,
        momentum=0.9,
        weight_decay=5e-4,
        nesterov=True,  # Paper uses Nesterov momentum
    )

    # Learning rate schedule: divide by 5 at 60, 120, and 160 epochs
    milestones = [60, 120, 160]
    scheduler = optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=milestones, gamma=0.2  # 1/5 = 0.2
    )

    best_acc = 0.0
    model = model.to(device)

    for epoch in range(epochs):
        # Training
        model.train()
        train_loss = 0
        correct = 0
        total = 0

        pbar = tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs}")
        for inputs, targets in pbar:
            inputs, targets = inputs.to(device), targets.to(device)

            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()

            train_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()

            # Show current learning rate in progress bar
            current_lr = optimizer.param_groups[0]["lr"]
            pbar.set_postfix(
                {
                    "loss": f"{train_loss/total:.3f}",
                    "acc": f"{100.*correct/total:.2f}%",
                    "lr": f"{current_lr:.3e}",
                }
            )

        # Validation
        model.eval()
        val_loss = 0
        correct = 0
        total = 0

        with torch.no_grad():
            for inputs, targets in val_loader:
                inputs, targets = inputs.to(device), targets.to(device)
                outputs = model(inputs)
                loss = criterion(outputs, targets)

                val_loss += loss.item()
                _, predicted = outputs.max(1)
                total += targets.size(0)
                correct += predicted.eq(targets).sum().item()

        acc = 100.0 * correct / total
        logger.info(f"Epoch {epoch+1}: Val Acc: {acc:.2f}% (lr={current_lr:.3e})")

        # Save checkpoint if best accuracy
        if acc > best_acc:
            logger.info(f"Saving checkpoint... ({acc:.2f}%)")
            best_acc = acc
            state = {
                "model_state_dict": model.state_dict(),
                "acc": acc,
                "epoch": epoch,
            }
            Path(CHECKPOINT_PATH).parent.mkdir(parents=True, exist_ok=True)
            torch.save(state, CHECKPOINT_PATH)

        scheduler.step()

    logger.info(f"Best accuracy: {best_acc:.2f}%")
    return best_acc > 50  # Consider training successful if accuracy > 50%


def verify_checkpoint():
    """Verify that checkpoint exists or train model."""
    if not Path(CHECKPOINT_PATH).exists():
        logger.info(
            f"Checkpoint not found at {CHECKPOINT_PATH}, will train from scratch"
        )

        # Get datasets with proper transforms
        transform_train = transforms.Compose(
            [
                transforms.RandomCrop(32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.5071, 0.4867, 0.4408], std=[0.2675, 0.2565, 0.2761]
                ),
            ]
        )

        transform_test = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.5071, 0.4867, 0.4408], std=[0.2675, 0.2565, 0.2761]
                ),
            ]
        )

        train_dataset = get_dataset("cifar100", train=True, transform=transform_train)
        val_dataset = get_dataset("cifar100", train=False, transform=transform_test)

        # Create data loaders with paper settings
        train_loader = DataLoader(
            train_dataset,
            batch_size=128,  # Paper setting
            shuffle=True,
            num_workers=4,
            pin_memory=True,
            drop_last=True,  # Ensure consistent batch sizes for BN
        )
        val_loader = DataLoader(
            val_dataset, batch_size=128, shuffle=False, num_workers=4, pin_memory=True
        )

        # Create model
        model = get_model("cifar100", "wrn-28-10")
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Train model
        if not train_model(model, train_loader, val_loader, device):
            logger.error("Failed to train model with good accuracy")
            return False

    return True


def run_cifar100_experiment(subset_size=5000):
    """Run CIFAR-100 experiment."""
    logger.info(f"Step 1: Running CIFAR-100 experiment (subset_size={subset_size})")

    # Verify checkpoint exists or train model
    if not verify_checkpoint():
        return False

    try:
        # Create experiment
        cifar_exp = TraditionalExperiment(
            config=create_experiment_config(
                dataset_name="cifar100",
                model_name="wrn-28-10",
                checkpoint_path=CHECKPOINT_PATH,
            ),
            subset_size=subset_size,
        )

        # Load dataset
        train_dataset = get_dataset("cifar100", train=True)
        test_dataset = get_dataset("cifar100", train=False)

        # If using subset, take first subset_size samples
        if subset_size:
            train_dataset = torch.utils.data.Subset(
                train_dataset, range(min(subset_size, len(train_dataset)))
            )
            test_dataset = torch.utils.data.Subset(
                test_dataset, range(min(subset_size // 5, len(test_dataset)))
            )

        # Extract features
        train_features, train_labels = cifar_exp.extract_features(train_dataset)
        test_features, test_labels = cifar_exp.extract_features(test_dataset)

        # Train and evaluate classifiers
        results = {}
        for clf_name in cifar_exp.classifiers:
            logger.info(f"Training {clf_name}")
            train_time, inference_time, accuracy = cifar_exp.train_and_evaluate(
                clf_name, train_features, train_labels, test_features, test_labels
            )
            results[clf_name] = {
                "accuracy": accuracy,
                "train_time": train_time,
                "inference_time": inference_time,
            }

        # Log results
        logger.info("\nCIFAR-100 results:")
        for clf_name, metrics in results.items():
            logger.info(
                f"{clf_name}: {metrics['accuracy']:.2f}% "
                f"(Train: {metrics['train_time']:.2f}s, "
                f"Inference: {metrics['inference_time']:.2f}s)"
            )

        # Check if results are good enough
        if all(metrics["accuracy"] < 10.0 for metrics in results.values()):
            logger.error(
                "CIFAR-100 experiment failed or returned poor results, stopping here."
            )
            return False

        return True

    except Exception as e:
        logger.error(f"Error in CIFAR-100 experiment: {str(e)}")
        logger.error("Traceback:", exc_info=True)
        logger.error(
            "CIFAR-100 experiment failed or returned poor results, stopping here."
        )
        return False


def main():
    """Run example experiments."""
    # Set up logging
    setup_logging()

    # Create checkpoints directory
    Path("checkpoints").mkdir(exist_ok=True)

    # Run experiments with subset
    if not run_cifar100_experiment(subset_size=5000):
        sys.exit(1)


if __name__ == "__main__":
    main()
