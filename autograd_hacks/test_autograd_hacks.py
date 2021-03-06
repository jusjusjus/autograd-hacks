
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytest

from . import autograd_hacks


class StriddenNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 20, 5, stride=2, padding=2)
        self.conv2 = nn.Conv2d(20, 30, 5, stride=2, padding=2)
        self.fc1_input_size = 7 * 7 * 30
        self.fc1 = nn.Linear(self.fc1_input_size, 500)
        self.fc2 = nn.Linear(500, 10)

    def forward(self, x):
        batch_size = x.shape[0]
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = x.view(batch_size, self.fc1_input_size)
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


class SimpleNet(nn.Module):
    """Lenet-5 from https://github.com/pytorch/examples/blob/master/mnist/main.py"""
    def __init__(self):
        super().__init__()
        self.linear = nn.Linear(28 * 28, 10)

    def forward(self, x):
        x = torch.flatten(x, 1)
        return self.linear(x)


class Net(nn.Module):
    """Lenet-5 from https://github.com/pytorch/examples/blob/master/mnist/main.py"""
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 20, 5)
        self.conv2 = nn.Conv2d(20, 50, 5)
        self.fc1 = nn.Linear(4 * 4 * 50, 500)
        self.fc2 = nn.Linear(500, 10)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.max_pool2d(x, 2, 2)
        x = F.relu(self.conv2(x))
        x = F.max_pool2d(x, 2, 2)
        x = x.view(-1, 4 * 4 * 50)
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


class TinyNet(nn.Module):
    """Tiny LeNet-5 for Hessian testing"""

    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 2, 2, 1)
        self.conv2 = nn.Conv2d(2, 2, 2, 1)
        self.fc1 = nn.Linear(2, 2)
        self.fc2 = nn.Linear(2, 10)

    def forward(self, x):            # 28x28
        x = F.max_pool2d(x, 4, 4)    # 7x7
        x = F.relu(self.conv1(x))    # 6x6
        x = F.max_pool2d(x, 2, 2)    # 3x3
        x = F.relu(self.conv2(x))    # 2x2
        x = F.max_pool2d(x, 2, 2)    # 1x1
        x = x.view(-1, 2 * 1 * 1)    # C * W * H
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


# Autograd helpers, from https://gist.github.com/apaszke/226abdf867c4e9d6698bd198f3b45fb7
def jacobian(y: torch.Tensor, x: torch.Tensor, create_graph=False):
    jac = []
    flat_y = y.reshape(-1)
    grad_y = torch.zeros_like(flat_y)
    for i in range(len(flat_y)):
        grad_y[i] = 1.
        grad_x, = torch.autograd.grad(flat_y, x, grad_y, retain_graph=True, create_graph=create_graph)
        jac.append(grad_x.reshape(x.shape))
        grad_y[i] = 0.
    return torch.stack(jac).reshape(y.shape + x.shape)


def hessian(y: torch.Tensor, x: torch.Tensor):
    return jacobian(jacobian(y, x, create_graph=True), x)


@pytest.mark.parametrize("Net", [Net, TinyNet, SimpleNet, StriddenNet])
def test_grad1(Net):
    torch.manual_seed(1)
    model = Net()
    loss_fn = nn.CrossEntropyLoss()

    n = 4
    data = torch.rand(n, 1, 28, 28)
    targets = torch.LongTensor(n).random_(0, 10)

    autograd_hacks.add_hooks(model)
    output = model(data)
    loss_fn(output, targets).backward(retain_graph=True)
    autograd_hacks.compute_grad1(model)
    autograd_hacks.disable_hooks()

    # Compare values against autograd
    losses = torch.stack([loss_fn(output[i:i+1], targets[i:i+1])
                          for i in range(len(data))])

    for layer in model.modules():
        if not autograd_hacks.is_supported(layer):
            continue

        for param in layer.parameters():
            assert torch.allclose(param.grad, param.grad1[0].mean(dim=0))
            assert torch.allclose(jacobian(losses, param), param.grad1[0])


def test_applying_backwards_twice_fails():
    torch.manual_seed(42)
    model = Net()
    loss_fn = nn.CrossEntropyLoss()

    data = torch.rand(5, 1, 28, 28)
    targets = torch.LongTensor(5).random_(0, 10)

    autograd_hacks.add_hooks(model)
    output = model(data)
    loss_fn(output, targets).backward()
    output = model(data)
    with pytest.raises(AssertionError):
        loss_fn(output, targets).backward()


def test_grad1_for_multiple_connected_passes():
    torch.manual_seed(42)
    model = SimpleNet()
    loss_fn = nn.CrossEntropyLoss(reduction='sum')

    def get_data(batch_size):
        return (torch.rand(batch_size, 1, 28, 28),
                torch.LongTensor(batch_size).random_(0, 10))

    n = 5
    autograd_hacks.add_hooks(model)

    data, targets = get_data(n)
    output = model(data)
    loss1 = loss_fn(output, targets)
    data, targets = get_data(n)
    output = model(data)
    loss2 = loss_fn(output, targets)
    loss = loss1 - loss2
    loss.backward()

    autograd_hacks.compute_grad1(model)
    autograd_hacks.disable_hooks()

    for n, p in model.named_parameters():
        grad1 = p.grad1[0] + p.grad1[1]
        assert p.grad.shape == grad1.shape[1:]
        assert torch.allclose(p.grad, grad1.mean(dim=0), atol=1e-7)


@pytest.mark.parametrize("hess_type", ['CrossEntropy', 'LeastSquares'])
def test_hess(hess_type):
    torch.manual_seed(1)
    model = TinyNet()

    def least_squares_loss(data_, targets_):
       assert len(data_) == len(targets_)
       err = data_ - targets_
       return torch.sum(err * err) / 2 / len(data_)

    n = 3
    data = torch.rand(n, 1, 28, 28)

    autograd_hacks.add_hooks(model)
    output = model(data)

    if hess_type == 'LeastSquares':
        targets = torch.rand(output.shape)
        loss_fn = least_squares_loss
    elif hess_type == 'CrossEntropy':
        targets = torch.LongTensor(n).random_(0, 10)
        loss_fn = nn.CrossEntropyLoss()
    else:
        raise ValueError(f"Unknown hessian type")

    autograd_hacks.backprop_hess(output, hess_type)
    autograd_hacks.clear_backprops(model)
    autograd_hacks.backprop_hess(output, hess_type)

    autograd_hacks.compute_hess(model)
    autograd_hacks.disable_hooks()

    for layer in model.modules():
        if not autograd_hacks.is_supported(layer):
            continue

        for param in layer.parameters():
            loss = loss_fn(output, targets)
            hess_autograd = hessian(loss, param)
            hess = param.hess
            assert torch.allclose(hess, hess_autograd.reshape(hess.shape))
