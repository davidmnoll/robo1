from setuptools import setup

package_name = 'rosmaster_robot'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', ['launch/robot.launch.py']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Maintainer',
    maintainer_email='you@example.com',
    description='Rosmaster A1 robot controller',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'controller = rosmaster_robot.controller:main',
            'audio_stream = rosmaster_robot.audio_stream:main',
            'audio_play = rosmaster_robot.audio_play:main',
        ],
    },
)
