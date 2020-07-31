import tensorflow as tf

from .utils import AffineTransform, Normalize2D, Repeat2D, Blur


class Generator(tf.keras.Model):
    """Generate image from style vector.
    """
    def __init__(self,
                 init_channels,
                 max_channels,
                 num_layer,
                 out_channels):
        """Initializer.
        Args:
            init_channels: int, number of the channels of global latent.
            max_channels: int, maximum channels of the feature map.
            num_layer: int, number of the layers, it determines the size of the image.
            out_channels: int, number of the image channels, 3 for rgb, 1 for gray-scale.
        """
        super(Generator, self).__init__()
        self.init_channels = init_channels
        self.max_channels = max_channels
        self.num_layer = num_layer
        self.out_channels = out_channels
        self.level = self.num_layer - 1

        resolution = 4
        channels = self.init_channels * 2 ** (self.num_layer - 1)
        out_dim = min(self.max_channels, channels)
        self.const = tf.ones([1, resolution, resolution, out_dim])

        self.blocks = []
        self.to_rgb = []
        for i in range(self.num_layer):
            out_dim = min(self.max_channels, channels)
            self.blocks.append(
                Generator.Block(out_dim,
                                i > 0,
                                'repeat' if resolution < 128 else 'deconv'))
            channels //= 2
            resolution *= 2
            self.to_rgb.append(tf.keras.layers.Conv2D(self.out_channels, 1, gain=0.03))
    
    def set_level(self, level):
        """Set training level, start from first block to last block.
        Args:
            level: int, training level, in range [0, num_layer).
        """
        self.level = level
    
    def level_variables(self):
        """Get trainable variables based on training level.
        Returns:
            List[tf.Variable], trainable variables.
        """
        var = self.to_rgb[self.level].trainable_variables
        for block in self.blocks[:self.level + 1]:
            var += block.trainable_variables
        return var

    def call(self, styles):
        """Generate image.
        Args:
            styles: tf.Tensor, [B, latent_dim], style vector.
        Returns:
            tf.Tensor, [B, S, S, out_channels], generated image.
                where S = 2 ** (num_layer + 1).
                if self.level is set, S = 2 ** (level + 2).
        """
        # [B, 4, 4, in_dim]
        x = self.const
        for block in self.blocks[:self.level + 1]:
            # [B, S', S', C] where S' = 4 * 2 ** i, C = in_dim / (2 ** (i + 1))
            x = block(x, styles, styles)
        # [B, S, S, out_channels]
        return self.to_rgb[self.level](x)

    class Block(tf.keras.Model):
        """Generator block for progressive growing.
        """
        def __init__(self, out_dim, preconv, upsample):
            """Initializer.
            Args:
                out_dim: int, number of channels of output feature map.
                preconv: bool, whether run convolution for upsampling.
                upsample: str, upsampling policy for pre-convolution.
                    - repeat: (2x2, repeat)-upsample -> (3x3, stride=1)-conv
                    - deconv: (3x3, stride=2)-conv2d transpose
            """
            super(Generator.Block, self).__init__()
            self.out_dim = out_dim
            self.preconv = preconv
            self.upsample = upsample

            if self.preconv:
                if self.upsample == 'repeat':
                    self.upsample_conv = tf.keras.Sequential([
                        Repeat2D(2),
                        tf.keras.layers.Conv2D(
                            self.out_dim, 3,
                            strides=1,
                            padding='SAME',
                            use_bias=False)])
                elif self.upsample == 'deconv':
                    self.upsample_conv = tf.keras.layers.Conv2DTranspose(
                        self.out_dim, 3, 2,
                        padding='SAME', use_bias=False, transform_kernel=True)

                self.blur = Blur()
    
            self.leaky_relu = tf.keras.layers.LeakyReLU(0.2)
            self.normalize = Normalize2D()

            self.noise_affine1 = AffineTransform([1, 1, 1, self.out_dim])
            self.latent_proj1 = tf.keras.layers.Dense(self.out_dim * 2, gain=1)

            self.conv = tf.keras.layers.Conv2D(
                self.out_dim, 3, 1, padding='SAME', use_bias=False)
            
            self.noise_affine2 = AffineTransform([1, 1, 1, self.out_dim])
            self.latent_proj2 = tf.keras.layers.Dense(self.out_dim * 2, gain=1)

        def call(self, x, s1, s2):
            """Generate next level feature map.
            Args:
                x: tf.Tensor, [B, H, W, in_dim], input feature map.
                s1: tf.Tensor, [B, latent_dim], first style.
                s2: tf.Tensor, [B, latent_dim], second style.
            Returns:
                tf.Tensor, [B, Hx2, Wx2, out_dim], next feature map.
            """
            if self.preconv:
                # [B, Hx2, Wx2, out_dim]
                x = self.upsample_conv(x)
                x = self.blur(x)

            shape = tf.shape(x)
            # [1, Hx2, Wx2, 1]
            noise = tf.random.normal([1, shape[1], shape[2], 1])
            # [B, Hx2, Wx2, out_dim]
            x = self.leaky_relu(x + self.noise_affine1(noise))
            # [B, Hx2, Wx2, out_dim]
            x = self.normalize(x)

            # [B, out_dim x 2]
            s1 = self.latent_proj1(s1)
            # [B, 2, out_dim]
            s1 = tf.reshape(s1, [-1, 2, self.out_dim])
            # [B, Hx2, Wx2, out_dim]
            x = s1[:, 0, None, None, :] + x * s1[:, 1, None, None, :]

            # [B, Hx2, Wx2, out_dim]
            x = self.conv(x)

            # [1, Hx2, Wx2, 1]
            noise = tf.random.normal([1, shape[1], shape[2], 1])
            # [B, Hx2, Wx2, out_dim]
            x = self.leaky_relu(x + self.noise_affine2(noise))
            # [B, Hx2, Wx2, out_dim]
            x = self.normalize(x)

            # [B, out_dim x 2]
            s2 = self.latent_proj2(s2)
            # [B, 2, out_dim]
            s2 = tf.reshape(s2, [-1, 2, self.out_dim])
            # [B, Hx2, Wx2, out_dim]
            x = s2[:, 0, None, None, :] + x * s2[:, 1, None, None, :]

            return x
